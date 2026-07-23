import discord
from discord.ext import commands, tasks
import os
import json
import aiohttp
import random
import datetime
from datetime import timezone
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# ═══════════════════════════════════════════════════════════════════════════════
#  AYARLAR — buradan kolayca düzenleyebilirsin
# ═══════════════════════════════════════════════════════════════════════════════

LOG_CHANNEL_ID     = 1529951018916249662  # ← Log kanalının ID'si
HEDEF_OYUN_ID      = 77807741669217       # Yalnızca bu oyunda mesai sayılır
HAFTALIK_KOTA_SAATI = 7                   # Haftalık hedef (saat)

KAYIT_DOSYASI  = "kayitlar.json"          # Discord ID <-> Roblox bilgileri
KONTROL_SURESI = 5                        # Kaç saniyede bir varlık kontrolü

# ═══════════════════════════════════════════════════════════════════════════════


# ── Kalıcı depolama ──────────────────────────────────────────────────────────

def kayitlari_yukle() -> dict:
    """JSON dosyasından kayıtları yükle; eksik haftalik_sure alanını doldur."""
    if os.path.exists(KAYIT_DOSYASI):
        with open(KAYIT_DOSYASI, "r", encoding="utf-8") as f:
            veri = json.load(f)
        # Eski kayıtlarda haftalik_sure yoksa ekle
        for bilgi in veri.values():
            bilgi.setdefault("haftalik_sure", 0)
        return veri
    return {}

def kayitlari_kaydet(data: dict):
    """Kayıtları JSON dosyasına yaz."""
    with open(KAYIT_DOSYASI, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# Yapı: { "discord_id_str": { "roblox_id": int, "roblox_kullanici_adi": str, "haftalik_sure": int } }
kayitlar: dict = kayitlari_yukle()

# Oyun oturumu takibi: { roblox_id_int: datetime (UTC) }
oyun_baslangici: dict[int, datetime.datetime] = {}


# ── Keep-Alive Web Sunucusu (UptimeRobot için) ────────────────────────────────

class _KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = "Bot aktif ve uyanık!".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # Konsolda istek gürültüsünü bastır

def keep_alive():
    server = HTTPServer(("0.0.0.0", 8082), _KeepAliveHandler)
    server.serve_forever()

_web_thread = threading.Thread(target=keep_alive, daemon=True)
_web_thread.start()

# ── Bot kurulumu ─────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


# ── Roblox API yardımcıları ──────────────────────────────────────────────────

ROBLOX_ARAMA_URL  = "https://users.roblox.com/v1/users/search"
ROBLOX_VARLIK_URL = "https://presence.roblox.com/v1/presence/users"

async def roblox_id_bul(kullanici_adi: str) -> tuple[int, str] | None:
    """Kullanıcı adına göre Roblox ID ve gerçek adı döndürür."""
    params = {"keyword": kullanici_adi, "limit": 10}
    async with aiohttp.ClientSession() as session:
        async with session.get(ROBLOX_ARAMA_URL, params=params) as resp:
            if resp.status != 200:
                return None
            veri = await resp.json()

    for kullanici in veri.get("data", []):
        if kullanici.get("name", "").lower() == kullanici_adi.lower():
            return kullanici["id"], kullanici["name"]

    ilk = veri.get("data", [None])[0]
    if ilk:
        return ilk["id"], ilk["name"]
    return None


async def varlik_sorgula(roblox_idler: list[int]) -> list[dict]:
    """Toplu varlık sorgusu. userPresenceType: 0=Çevrimdışı, 1=Çevrimiçi, 2=Oyunda, 3=Studio"""
    if not roblox_idler:
        return []
    async with aiohttp.ClientSession() as session:
        async with session.post(
            ROBLOX_VARLIK_URL,
            json={"userIds": roblox_idler}
        ) as resp:
            if resp.status != 200:
                return []
            veri = await resp.json()
    return veri.get("userPresences", [])


# ── Yardımcı fonksiyonlar ────────────────────────────────────────────────────

def sure_formatla(saniye: int) -> str:
    """Saniyeyi 'X saat Y dakika Z saniye' formatına çevirir."""
    saat   = saniye // 3600
    dakika = (saniye % 3600) // 60
    sn     = saniye % 60
    parcalar = []
    if saat:   parcalar.append(f"{saat} saat")
    if dakika: parcalar.append(f"{dakika} dakika")
    parcalar.append(f"{sn} saniye")
    return " ".join(parcalar)

def kota_ilerleme_cubugu(mevcut_sn: int, hedef_sn: int, uzunluk: int = 10) -> str:
    """İlerleme çubuğu oluşturur. Örnek: ▰▰▰▰▰▱▱▱▱▱"""
    oran    = min(mevcut_sn / hedef_sn, 1.0) if hedef_sn > 0 else 0
    dolu    = round(oran * uzunluk)
    bos     = uzunluk - dolu
    return "▰" * dolu + "▱" * bos

def kota_tamamlandi_mi(bilgi: dict) -> bool:
    hedef_sn = HAFTALIK_KOTA_SAATI * 3600
    return bilgi.get("haftalik_sure", 0) >= hedef_sn


# ── Arka plan: varlık kontrolü ───────────────────────────────────────────────

@tasks.loop(seconds=KONTROL_SURESI)
async def varlik_kontrol():
    """Her KONTROL_SURESI saniyede bir tüm kayıtlı kullanıcıları sorgular."""
    if not kayitlar:
        return

    id_discord_esles = {
        bilgi["roblox_id"]: discord_id
        for discord_id, bilgi in kayitlar.items()
    }
    roblox_idler = list(id_discord_esles.keys())

    varliklar = await varlik_sorgula(roblox_idler)
    simdi = datetime.datetime.now(timezone.utc)

    for varlik in varliklar:
        roblox_id    = varlik.get("userId")
        tur          = varlik.get("userPresenceType", 0)
        discord_id   = id_discord_esles.get(roblox_id)

        if discord_id is None:
            continue

        kullanici_adi = kayitlar[discord_id]["roblox_kullanici_adi"]

        root_place_id = varlik.get("rootPlaceId")
        place_id      = varlik.get("placeId")
        hedef_oyunda  = (
            tur == 2 and
            (root_place_id == HEDEF_OYUN_ID or place_id == HEDEF_OYUN_ID)
        )

        if hedef_oyunda and roblox_id not in oyun_baslangici:
            # Hedef oyuna yeni girdi → oturum başlat
            oyun_baslangici[roblox_id] = simdi

        elif not hedef_oyunda and roblox_id in oyun_baslangici:
            # Oyundan çıktı → süreyi hesapla, kaydet, log at
            baslangic  = oyun_baslangici.pop(roblox_id)
            sure_sn    = int((simdi - baslangic).total_seconds())
            sure_metin = sure_formatla(sure_sn)

            # Haftalık süreye ekle
            kayitlar[discord_id]["haftalik_sure"] = (
                kayitlar[discord_id].get("haftalik_sure", 0) + sure_sn
            )
            kayitlari_kaydet(kayitlar)

            kanal = bot.get_channel(LOG_CHANNEL_ID)
            if kanal is None:
                print(f"[UYARI] LOG_CHANNEL_ID ({LOG_CHANNEL_ID}) bulunamadı!")
                continue

            haftalik_sn    = kayitlar[discord_id]["haftalik_sure"]
            hedef_sn       = HAFTALIK_KOTA_SAATI * 3600
            tamamlandi     = haftalik_sn >= hedef_sn
            cubuk          = kota_ilerleme_cubugu(haftalik_sn, hedef_sn)
            haftalik_metin = sure_formatla(haftalik_sn)
            kota_durum     = "✅ Kota Tamamlandı!" if tamamlandi else f"{cubuk} {haftalik_metin} / {HAFTALIK_KOTA_SAATI} saat"

            embed = discord.Embed(
                title="📋 Mesai Sonu",
                color=discord.Color.red(),
                timestamp=simdi
            )
            embed.set_author(name=f"{kullanici_adi} — Roblox")
            embed.add_field(name="Discord",          value=f"<@{discord_id}>",                                    inline=True)
            embed.add_field(name="Roblox Kullanıcı", value=f"`{kullanici_adi}`",                                  inline=True)
            embed.add_field(name="Oynanan Oyun",     value="`Adana RP`",                                          inline=False)
            embed.add_field(name="Mesai Başlangıcı", value=discord.utils.format_dt(baslangic, style="T"),         inline=True)
            embed.add_field(name="Mesai Bitişi",     value=discord.utils.format_dt(simdi, style="T"),             inline=True)
            embed.add_field(name="Bu Oturum",        value=f"**{sure_metin}**",                                   inline=False)
            embed.add_field(name="📊 Haftalık Kota", value=kota_durum,                                            inline=False)
            embed.set_footer(text=f"Roblox ID: {roblox_id}")
            await kanal.send(embed=embed)


@varlik_kontrol.before_loop
async def varlik_oncesi():
    await bot.wait_until_ready()


# ── Arka plan: haftalık sıfırlama (her gün 23:59 UTC'de çalışır, Pazar kontrolü) ──

@tasks.loop(time=datetime.time(hour=23, minute=59, tzinfo=timezone.utc))
async def haftalik_sifirla_gorev():
    """Pazar 23:59 UTC'de tüm haftalık süreleri sıfırlar."""
    simdi = datetime.datetime.now(timezone.utc)
    if simdi.weekday() != 6:   # 6 = Pazar
        return

    for bilgi in kayitlar.values():
        bilgi["haftalik_sure"] = 0
    kayitlari_kaydet(kayitlar)
    print("[BİLGİ] Haftalık kotalar otomatik sıfırlandı.")

    kanal = bot.get_channel(LOG_CHANNEL_ID)
    if kanal:
        embed = discord.Embed(
            title="🔄 Haftalık Kota Sıfırlandı",
            description="Tüm jandarmaların haftalık mesai süreleri sıfırlandı. Yeni hafta başlıyor!",
            color=discord.Color.orange(),
            timestamp=simdi
        )
        await kanal.send(embed=embed)


@haftalik_sifirla_gorev.before_loop
async def sifirla_oncesi():
    await bot.wait_until_ready()


# ── Olaylar ──────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"{bot.user} olarak giriş yapıldı (ID: {bot.user.id})")
    print(f"{len(bot.guilds)} sunucuya bağlandı.")
    print(f"Kayıtlı kullanıcı sayısı: {len(kayitlar)}")
    varlik_kontrol.start()
    haftalik_sifirla_gorev.start()
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching,
        name="!yardim için"
    ))


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.send("❓ Bilinmeyen komut. Mevcut komutları görmek için `!yardim` yazın.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠️ Eksik argüman: `{error.param.name}`. Kullanım için `!yardim {ctx.command}` yazın.")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("🚫 Bu komutu kullanmak için yetkiniz yok.")
    else:
        await ctx.send(f"❌ Bir hata oluştu: {error}")
        raise error


# ── Kayıt ve Profil Komutları ─────────────────────────────────────────────────

@bot.command(name="kayit")
async def kayit(ctx, roblox_kullanici_adi: str):
    """Roblox kullanıcı adınızı Discord hesabınıza bağlar."""
    bekle_msg = await ctx.send(f"🔍 `{roblox_kullanici_adi}` aranıyor...")

    sonuc = await roblox_id_bul(roblox_kullanici_adi)
    if sonuc is None:
        await bekle_msg.edit(content=f"❌ `{roblox_kullanici_adi}` adlı Roblox kullanıcısı bulunamadı.")
        return

    roblox_id, gercek_ad = sonuc
    discord_id_str = str(ctx.author.id)

    # Önceki kayıt varsa haftalik_sure'yi koru
    mevcut_sure = kayitlar.get(discord_id_str, {}).get("haftalik_sure", 0)
    kayitlar[discord_id_str] = {
        "roblox_id": roblox_id,
        "roblox_kullanici_adi": gercek_ad,
        "haftalik_sure": mevcut_sure
    }
    kayitlari_kaydet(kayitlar)

    embed = discord.Embed(title="✅ Kayıt Başarılı", color=discord.Color.green())
    embed.add_field(name="Discord",          value=ctx.author.mention, inline=True)
    embed.add_field(name="Roblox Kullanıcı", value=f"`{gercek_ad}`",   inline=True)
    embed.add_field(name="Roblox ID",        value=f"`{roblox_id}`",    inline=True)
    embed.set_footer(text="Mesai takibi artık aktif!")
    await bekle_msg.edit(content=None, embed=embed)


@bot.command(name="profilim")
async def profilim(ctx):
    """Kayıtlı Roblox bilgilerinizi gösterir."""
    bilgi = kayitlar.get(str(ctx.author.id))
    if not bilgi:
        await ctx.send("❌ Kayıtlı bir Roblox hesabınız yok. `!kayit <kullanici_adi>` ile kayıt olun.")
        return

    hedef_sn       = HAFTALIK_KOTA_SAATI * 3600
    haftalik_sn    = bilgi.get("haftalik_sure", 0)
    tamamlandi     = haftalik_sn >= hedef_sn
    cubuk          = kota_ilerleme_cubugu(haftalik_sn, hedef_sn)
    haftalik_metin = sure_formatla(haftalik_sn)

    embed = discord.Embed(title="👤 Roblox Profiliniz", color=discord.Color.blurple())
    embed.add_field(name="Discord",          value=ctx.author.mention,                    inline=True)
    embed.add_field(name="Roblox Kullanıcı", value=f"`{bilgi['roblox_kullanici_adi']}`",  inline=True)
    embed.add_field(name="Roblox ID",        value=f"`{bilgi['roblox_id']}`",              inline=True)

    oyun_aktif = bilgi["roblox_id"] in oyun_baslangici
    embed.add_field(name="Mesai Durumu", value="🟢 Oyunda" if oyun_aktif else "🔴 Oyun Dışı", inline=False)
    if oyun_aktif:
        baslangic = oyun_baslangici[bilgi["roblox_id"]]
        embed.add_field(name="Oturum Başlangıcı", value=discord.utils.format_dt(baslangic, style="R"), inline=True)

    kota_durum = "✅ Tamamlandı!" if tamamlandi else f"{cubuk}\n{haftalik_metin} / {HAFTALIK_KOTA_SAATI} saat"
    embed.add_field(name="📊 Haftalık Kota", value=kota_durum, inline=False)
    await ctx.send(embed=embed)


@bot.command(name="kayitlar")
async def kayit_listesi(ctx):
    """Sunucudaki tüm kayıtlı kullanıcıları listeler."""
    if not kayitlar:
        await ctx.send("📭 Henüz kayıtlı kullanıcı yok.")
        return

    satirlar = []
    for discord_id_str, bilgi in kayitlar.items():
        oyun_ikon = "🟢" if bilgi["roblox_id"] in oyun_baslangici else "⚫"
        satirlar.append(f"{oyun_ikon} <@{discord_id_str}> → `{bilgi['roblox_kullanici_adi']}`")

    embed = discord.Embed(
        title=f"📋 Kayıtlı Kullanıcılar ({len(kayitlar)})",
        description="\n".join(satirlar),
        color=discord.Color.blue()
    )
    embed.set_footer(text="🟢 Oyunda  ⚫ Oyun Dışı")
    await ctx.send(embed=embed)


@bot.command(name="kayitkaldir")
async def kayit_kaldir(ctx):
    """Roblox hesap bağlantınızı kaldırır."""
    discord_id_str = str(ctx.author.id)
    if discord_id_str not in kayitlar:
        await ctx.send("❌ Zaten kayıtlı bir hesabınız yok.")
        return
    bilgi = kayitlar.pop(discord_id_str)
    oyun_baslangici.pop(bilgi["roblox_id"], None)
    kayitlari_kaydet(kayitlar)
    await ctx.send(f"✅ `{bilgi['roblox_kullanici_adi']}` bağlantısı kaldırıldı.")


# ── Kota Komutları ────────────────────────────────────────────────────────────

@bot.command(name="kota")
async def kota(ctx):
    """Bu haftaki mesai kotanızı gösterir."""
    bilgi = kayitlar.get(str(ctx.author.id))
    if not bilgi:
        await ctx.send("❌ Kayıtlı bir Roblox hesabınız yok. `!kayit <kullanici_adi>` ile kayıt olun.")
        return

    hedef_sn       = HAFTALIK_KOTA_SAATI * 3600
    haftalik_sn    = bilgi.get("haftalik_sure", 0)
    tamamlandi     = haftalik_sn >= hedef_sn
    oran           = min(haftalik_sn / hedef_sn, 1.0) if hedef_sn > 0 else 0
    cubuk          = kota_ilerleme_cubugu(haftalik_sn, hedef_sn)
    haftalik_metin = sure_formatla(haftalik_sn)
    kalan_sn       = max(hedef_sn - haftalik_sn, 0)
    kalan_metin    = sure_formatla(kalan_sn)
    yuzde          = round(oran * 100)

    renk = discord.Color.green() if tamamlandi else (
        discord.Color.yellow() if oran >= 0.5 else discord.Color.red()
    )

    embed = discord.Embed(
        title="📊 Haftalık Kota Durumunuz",
        color=renk
    )
    embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
    embed.add_field(name="Roblox",         value=f"`{bilgi['roblox_kullanici_adi']}`", inline=True)
    embed.add_field(name="Hedef",          value=f"`{HAFTALIK_KOTA_SAATI} saat`",      inline=True)
    embed.add_field(name="Tamamlanma",     value=f"`%{yuzde}`",                        inline=True)
    embed.add_field(name="İlerleme",       value=f"{cubuk}\n**{haftalik_metin}** oynandı", inline=False)

    if tamamlandi:
        embed.add_field(name="Durum", value="✅ **Kotanızı tamamladınız! Tebrikler!**", inline=False)
    else:
        embed.add_field(name="Kalan Süre", value=f"⏳ `{kalan_metin}` daha oynamanız gerekiyor.", inline=False)

    embed.set_footer(text="Kotalar her Pazar 23:59'da sıfırlanır.")
    await ctx.send(embed=embed)


@bot.command(name="kotarapor")
@commands.has_permissions(administrator=True)
async def kotarapor(ctx):
    """[YÖNETİCİ] Tüm jandarmaların haftalık kota durumunu listeler."""
    if not kayitlar:
        await ctx.send("📭 Henüz kayıtlı kullanıcı yok.")
        return

    hedef_sn = HAFTALIK_KOTA_SAATI * 3600

    # Süreye göre azalan sırala
    sirali = sorted(
        kayitlar.items(),
        key=lambda x: x[1].get("haftalik_sure", 0),
        reverse=True
    )

    satirlar = []
    tamamlayanlar = 0
    for discord_id_str, bilgi in sirali:
        haftalik_sn = bilgi.get("haftalik_sure", 0)
        tamamlandi  = haftalik_sn >= hedef_sn
        if tamamlandi:
            tamamlayanlar += 1
        ikon        = "🟢" if tamamlandi else "🔴"
        sure_yazi   = sure_formatla(haftalik_sn)
        satirlar.append(
            f"{ikon} <@{discord_id_str}> — `{bilgi['roblox_kullanici_adi']}` → **{sure_yazi}**"
        )

    embed = discord.Embed(
        title=f"📋 Haftalık Kota Raporu — {HAFTALIK_KOTA_SAATI} Saat Hedef",
        description="\n".join(satirlar),
        color=discord.Color.blue(),
        timestamp=datetime.datetime.now(timezone.utc)
    )
    embed.set_footer(
        text=f"✅ Tamamlayan: {tamamlayanlar}/{len(kayitlar)}  |  "
             f"🔴 Eksik: {len(kayitlar) - tamamlayanlar}/{len(kayitlar)}"
    )
    await ctx.send(embed=embed)


@bot.command(name="kotasifirla")
@commands.has_permissions(administrator=True)
async def kotasifirla(ctx):
    """[YÖNETİCİ] Tüm haftalık süreleri manuel olarak sıfırlar."""
    for bilgi in kayitlar.values():
        bilgi["haftalik_sure"] = 0
    kayitlari_kaydet(kayitlar)

    embed = discord.Embed(
        title="🔄 Haftalık Kotalar Sıfırlandı",
        description=f"Tüm **{len(kayitlar)}** kullanıcının haftalık mesai süresi `{ctx.author.display_name}` tarafından sıfırlandı.",
        color=discord.Color.orange(),
        timestamp=datetime.datetime.now(timezone.utc)
    )
    await ctx.send(embed=embed)


# ── Genel Komutlar ────────────────────────────────────────────────────────────

@bot.command(name="gecikme")
async def gecikme(ctx):
    """Botun gecikmesini gösterir."""
    ms = round(bot.latency * 1000)
    embed = discord.Embed(
        title="🏓 Pong!",
        description=f"Gecikme: **{ms}ms**",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed)


@bot.command(name="yardim")
async def yardim(ctx, komut: str = None):
    """Mevcut komutları veya belirli bir komutun detaylarını gösterir."""
    if komut:
        cmd = bot.get_command(komut)
        if cmd is None:
            await ctx.send(f"❓ `{komut}` komutu bulunamadı.")
            return
        embed = discord.Embed(
            title=f"Yardım: !{cmd.name}",
            description=cmd.help or "Açıklama mevcut değil.",
            color=discord.Color.blurple()
        )
        await ctx.send(embed=embed)
        return

    embed = discord.Embed(
        title="📖 Bot Komutları",
        description="Ön ek: `!`  |  Detay için `!yardim <komut>` yazın.",
        color=discord.Color.blurple()
    )
    embed.add_field(name="🎮 Roblox & Mesai", value=(
        "`!kayit <kullanici_adi>` — Roblox hesabınızı bağlayın\n"
        "`!profilim` — Kayıtlı profilinizi görün\n"
        "`!kayitlar` — Tüm kayıtlı kullanıcılar\n"
        "`!kayitkaldir` — Bağlantıyı kaldırın"
    ), inline=False)
    embed.add_field(name="📊 Haftalık Kota", value=(
        "`!kota` — Haftalık kota durumunuzu görün\n"
        "`!kotarapor` — Tüm rapor [Yönetici]\n"
        "`!kotasifirla` — Manuel sıfırla [Yönetici]"
    ), inline=False)
    embed.add_field(name="🔧 Genel", value=(
        "`!gecikme` — Gecikmeyi kontrol eder\n"
        "`!yardim` — Bu menüyü gösterir\n"
        "`!yankila <metin>` — Bir mesajı tekrar eder\n"
        "`!soyle <metin>` — Bot bir şey söyler"
    ), inline=False)
    embed.add_field(name="📊 Sunucu", value=(
        "`!sunucubilgi` — Sunucu bilgisini gösterir\n"
        "`!kullanicibilgi [@kullanici]` — Kullanıcı bilgisini gösterir\n"
        "`!uyesayisi` — Üye sayısını gösterir"
    ), inline=False)
    embed.add_field(name="🎲 Eğlence", value=(
        "`!zar [yuz]` — Zar atar (varsayılan: 6)\n"
        "`!yazitura` — Yazı tura atar\n"
        "`!sec <a> | <b> | ...` — Seçenekler arasından seçer"
    ), inline=False)
    embed.set_footer(text=f"İsteyen: {ctx.author.display_name}")
    await ctx.send(embed=embed)


@bot.command(name="yankila")
async def yankila(ctx, *, metin: str):
    """Bir mesajı tekrar eder."""
    await ctx.send(metin)


@bot.command(name="soyle")
@commands.has_permissions(manage_messages=True)
async def soyle(ctx, *, metin: str):
    """Botun bir şey söylemesini sağlar (komut mesajınızı siler)."""
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass
    await ctx.send(metin)


# ── Sunucu Komutları ──────────────────────────────────────────────────────────

@bot.command(name="sunucubilgi")
async def sunucubilgi(ctx):
    """Mevcut sunucu hakkında bilgi gösterir."""
    guild = ctx.guild
    embed = discord.Embed(
        title=guild.name,
        description=guild.description or "Açıklama yok.",
        color=discord.Color.blue()
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="👑 Sahip",       value=guild.owner.mention,                    inline=True)
    embed.add_field(name="🌍 Bölge",       value=str(guild.preferred_locale),            inline=True)
    embed.add_field(name="📅 Oluşturulma", value=guild.created_at.strftime("%d %b %Y"),  inline=True)
    embed.add_field(name="👥 Üyeler",      value=guild.member_count,                     inline=True)
    embed.add_field(name="💬 Kanallar",    value=len(guild.channels),                    inline=True)
    embed.add_field(name="🎭 Roller",      value=len(guild.roles),                       inline=True)
    embed.set_footer(text=f"ID: {guild.id}")
    await ctx.send(embed=embed)


@bot.command(name="kullanicibilgi")
async def kullanicibilgi(ctx, uye: discord.Member = None):
    """Bir kullanıcı hakkında bilgi gösterir. Varsayılan olarak kendinizi gösterir."""
    uye = uye or ctx.author
    roller = [r.mention for r in uye.roles if r.name != "@everyone"]
    embed = discord.Embed(
        title=str(uye),
        color=uye.color if uye.color.value else discord.Color.blurple()
    )
    embed.set_thumbnail(url=uye.display_avatar.url)
    embed.add_field(name="🪪 Görünen Ad",       value=uye.display_name,                       inline=True)
    embed.add_field(name="🤖 Bot",               value="Evet" if uye.bot else "Hayır",         inline=True)
    embed.add_field(name="📅 Sunucuya Katılım",  value=uye.joined_at.strftime("%d %b %Y"),     inline=True)
    embed.add_field(name="📅 Hesap Oluşturma",   value=uye.created_at.strftime("%d %b %Y"),    inline=True)
    embed.add_field(
        name=f"🎭 Roller ({len(roller)})",
        value=", ".join(roller) if roller else "Yok",
        inline=False
    )
    embed.set_footer(text=f"ID: {uye.id}")
    await ctx.send(embed=embed)


@bot.command(name="uyesayisi")
async def uyesayisi(ctx):
    """Sunucudaki üye sayısını gösterir."""
    guild = ctx.guild
    insanlar = sum(1 for m in guild.members if not m.bot)
    botlar = guild.member_count - insanlar
    embed = discord.Embed(title=f"👥 {guild.name} Üyeleri", color=discord.Color.blue())
    embed.add_field(name="Toplam",   value=guild.member_count, inline=True)
    embed.add_field(name="İnsanlar", value=insanlar,           inline=True)
    embed.add_field(name="Botlar",   value=botlar,             inline=True)
    await ctx.send(embed=embed)


# ── Eğlence Komutları ─────────────────────────────────────────────────────────

@bot.command(name="zar")
async def zar(ctx, yuz: int = 6):
    """Zar atar. Varsayılan olarak 6 yüzlüdür."""
    if yuz < 2:
        await ctx.send("⚠️ Zarın en az 2 yüzü olmalı!")
        return
    sonuc = random.randint(1, yuz)
    await ctx.send(f"🎲 **{sonuc}** geldi! (d{yuz})")


@bot.command(name="yazitura")
async def yazitura(ctx):
    """Yazı tura atar."""
    sonuc = random.choice(["Yazı 🪙", "Tura 🪙"])
    await ctx.send(f"**{sonuc}**!")


@bot.command(name="sec")
async def sec(ctx, *, secenekler: str):
    """`|` ile ayrılmış seçenekler arasından seçer. Örnek: `!sec pizza | tacos | sushi`"""
    liste = [s.strip() for s in secenekler.split("|") if s.strip()]
    if len(liste) < 2:
        await ctx.send("⚠️ Lütfen `|` ile ayrılmış en az 2 seçenek girin.")
        return
    secilen = random.choice(liste)
    await ctx.send(f"🎯 Seçimim: **{secilen}**")


# ── Yönetici Komutları ────────────────────────────────────────────────────────

@bot.command(name="özelduyuru")
@commands.has_permissions(administrator=True)
async def ozel_duyuru(ctx, *, mesaj: str):
    """Kayıtlı tüm kullanıcılara DM yoluyla özel duyuru gönderir. (Yalnızca yöneticiler)"""
    if not kayitlar:
        await ctx.send("⚠️ Henüz kayıtlı kullanıcı yok.")
        return

    # İlerleme mesajı
    bilgi = await ctx.send("📤 Duyuru gönderiliyor, lütfen bekleyin...")

    basarili   = 0
    basarisiz  = 0
    kapali_dm  = []

    embed = discord.Embed(
        title="📢 JKK | Jandarma Komando Komutanlığı Duyurusu",
        description=mesaj,
        color=discord.Color.dark_red(),
        timestamp=datetime.datetime.now(timezone.utc)
    )
    embed.set_footer(text=f"Gönderen: {ctx.author.display_name} • JKK Bot")
    embed.set_thumbnail(url=ctx.guild.icon.url if ctx.guild.icon else discord.Embed.Empty)

    for discord_id_str in kayitlar:
        try:
            discord_id = int(discord_id_str)
            uye = ctx.guild.get_member(discord_id) or await bot.fetch_user(discord_id)
            await uye.send(embed=embed)
            basarili += 1
        except discord.Forbidden:
            # DM kutusu kapalı
            basarisiz += 1
            try:
                uye_adi = ctx.guild.get_member(int(discord_id_str))
                kapali_dm.append(uye_adi.display_name if uye_adi else f"ID:{discord_id_str}")
            except Exception:
                kapali_dm.append(f"ID:{discord_id_str}")
        except Exception:
            basarisiz += 1
            kapali_dm.append(f"ID:{discord_id_str}")

    # Rapor embed'i
    rapor = discord.Embed(
        title="📊 Duyuru Gönderim Raporu",
        color=discord.Color.green() if basarisiz == 0 else discord.Color.orange(),
        timestamp=datetime.datetime.now(timezone.utc)
    )
    rapor.add_field(name="✅ Başarıyla Gönderildi", value=f"**{basarili}** kişi", inline=True)
    rapor.add_field(name="❌ Gönderilemedi",        value=f"**{basarisiz}** kişi", inline=True)

    if kapali_dm:
        rapor.add_field(
            name="🔒 DM Kutusu Kapalı Olanlar",
            value="\n".join(f"• {ad}" for ad in kapali_dm) or "—",
            inline=False
        )

    rapor.set_footer(text=f"Toplam kayıtlı: {len(kayitlar)} kişi")
    await bilgi.delete()
    await ctx.send(embed=rapor)


@ozel_duyuru.error
async def ozel_duyuru_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("🚫 Bu komutu kullanmak için **Yönetici** yetkisine sahip olmalısın.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("⚠️ Kullanım: `!özelduyuru <duyuru mesajı>`")


# ── Çalıştır ──────────────────────────────────────────────────────────────────

token = os.environ.get("DISCORD_TOKEN")
if not token:
    raise RuntimeError("DISCORD_TOKEN ortam değişkeni ayarlanmamış.")

bot.run(token)
