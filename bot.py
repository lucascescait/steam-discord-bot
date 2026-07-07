import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import json
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ─── Configurações ───────────────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CANAL_ID = int(os.getenv("CANAL_ID", "0"))          # Canal onde as promoções serão enviadas
DESCONTO_MINIMO = int(os.getenv("DESCONTO_MINIMO", "50"))  # % mínimo de desconto (padrão: 50%)
INTERVALO_HORAS = int(os.getenv("INTERVALO_HORAS", "6"))   # Verificar a cada X horas

# IDs dos jogos já enviados (evita duplicatas na sessão)
jogos_enviados = set()

# ─── Bot Setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ─── Steam API ────────────────────────────────────────────────────────────────
STEAM_API_URL = "https://store.steampowered.com/api/featuredcategories/?cc=br&l=portuguese"
STEAM_SEARCH_URL = "https://store.steampowered.com/api/storeappsearch/?term=&cc=br&l=portuguese"
STEAM_SPECIALS_URL = "https://store.steampowered.com/api/featuredcategories/?cc=br&l=portuguese"
STEAM_APP_URL = "https://store.steampowered.com/app/{appid}"
STEAM_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails?appids={appids}&cc=br&l=portuguese&filters=price_overview,basic"

async def buscar_promocoes_steam(desconto_minimo: int = 50) -> list[dict]:
    """Busca promoções na Steam via API."""
    jogos_em_promocao = []

    async with aiohttp.ClientSession() as session:
        # 1. Buscar categorias em destaque (inclui promoções)
        try:
            async with session.get(STEAM_SPECIALS_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
        except Exception as e:
            print(f"[ERRO] Falha ao acessar API da Steam: {e}")
            return []

        # Extrair jogos em promoção das categorias
        categorias_interesse = ["specials", "top_sellers", "new_releases"]
        appids_para_checar = []

        for cat in categorias_interesse:
            if cat in data:
                items = data[cat].get("items", [])
                for item in items:
                    appid = item.get("id")
                    discount = item.get("discount_percent", 0)

                    if appid and discount >= desconto_minimo:
                        preco_original = item.get("original_price", 0)
                        preco_final = item.get("final_price", 0)
                        nome = item.get("name", "Desconhecido")
                        header_img = item.get("header_image", "")

                        jogos_em_promocao.append({
                            "appid": appid,
                            "nome": nome,
                            "desconto": discount,
                            "preco_original": preco_original / 100 if preco_original else 0,
                            "preco_final": preco_final / 100 if preco_final else 0,
                            "imagem": header_img,
                            "url": STEAM_APP_URL.format(appid=appid),
                            "categoria": cat,
                        })

    # Remover duplicatas por appid
    vistos = set()
    resultado = []
    for jogo in jogos_em_promocao:
        if jogo["appid"] not in vistos:
            vistos.add(jogo["appid"])
            resultado.append(jogo)

    # Ordenar por maior desconto
    resultado.sort(key=lambda x: x["desconto"], reverse=True)
    return resultado


def formatar_real(valor: float) -> str:
    """Formata valor em BRL."""
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def cor_desconto(desconto: int) -> discord.Color:
    """Retorna cor do embed baseada no desconto."""
    if desconto >= 80:
        return discord.Color.gold()
    elif desconto >= 60:
        return discord.Color.green()
    elif desconto >= 40:
        return discord.Color.teal()
    else:
        return discord.Color.blue()


def emoji_desconto(desconto: int) -> str:
    if desconto >= 90: return "🔥🔥🔥"
    if desconto >= 75: return "🔥🔥"
    if desconto >= 50: return "🔥"
    if desconto >= 25: return "💸"
    return "🏷️"


def criar_embed_jogo(jogo: dict) -> discord.Embed:
    """Cria um embed rico para um jogo em promoção."""
    desconto = jogo["desconto"]
    emoji = emoji_desconto(desconto)
    cor = cor_desconto(desconto)

    embed = discord.Embed(
        title=f"{emoji} {jogo['nome']}",
        url=jogo["url"],
        color=cor,
        timestamp=datetime.utcnow()
    )

    # Preços
    if jogo["preco_original"] > 0:
        embed.add_field(
            name="💰 Preço Original",
            value=f"~~{formatar_real(jogo['preco_original'])}~~",
            inline=True
        )
        embed.add_field(
            name="✅ Preço com Desconto",
            value=f"**{formatar_real(jogo['preco_final'])}**",
            inline=True
        )
        economia = jogo["preco_original"] - jogo["preco_final"]
        embed.add_field(
            name="📉 Desconto",
            value=f"**-{desconto}%** (economia de {formatar_real(economia)})",
            inline=True
        )
    else:
        embed.add_field(name="💰 Preço", value="**GRÁTIS!** 🎉", inline=False)

    # Imagem
    if jogo.get("imagem"):
        embed.set_image(url=jogo["imagem"])

    embed.set_footer(text="Steam BR • Clique no título para ir à loja")
    return embed


def criar_embed_resumo(jogos: list[dict]) -> discord.Embed:
    """Cria um embed de resumo com todos os jogos em promoção."""
    embed = discord.Embed(
        title="🎮 Promoções da Steam — Resumo",
        description=f"**{len(jogos)} jogos** em promoção acima do desconto mínimo configurado!",
        color=discord.Color.from_rgb(23, 153, 232),
        timestamp=datetime.utcnow()
    )

    for jogo in jogos[:10]:  # Top 10
        preco_txt = formatar_real(jogo["preco_final"]) if jogo["preco_final"] > 0 else "GRÁTIS"
        embed.add_field(
            name=f"{emoji_desconto(jogo['desconto'])} {jogo['nome'][:35]}",
            value=f"-{jogo['desconto']}% → **{preco_txt}**\n[Ver na Steam]({jogo['url']})",
            inline=True
        )

    if len(jogos) > 10:
        embed.set_footer(text=f"Steam BR • Mostrando top 10 de {len(jogos)} promoções")
    else:
        embed.set_footer(text="Steam BR • Clique nos links para ver na loja")

    return embed


# ─── Task: Verificar promoções automaticamente ────────────────────────────────
@tasks.loop(hours=INTERVALO_HORAS)
async def verificar_promocoes():
    await bot.wait_until_ready()
    canal = bot.get_channel(CANAL_ID)
    if not canal:
        print(f"[AVISO] Canal {CANAL_ID} não encontrado.")
        return

    print(f"[INFO] Buscando promoções da Steam (desconto mínimo: {DESCONTO_MINIMO}%)...")
    jogos = await buscar_promocoes_steam(DESCONTO_MINIMO)

    if not jogos:
        print("[INFO] Nenhuma promoção encontrada com os critérios atuais.")
        return

    # Filtrar jogos já enviados nesta sessão
    novos = [j for j in jogos if j["appid"] not in jogos_enviados]
    if not novos:
        print("[INFO] Nenhuma promoção nova para enviar.")
        return

    # Anúncio
    horario = datetime.now().strftime("%d/%m/%Y às %H:%M")
    await canal.send(
        f"🕹️ **Atualização de promoções — {horario}**\n"
        f"Encontrei **{len(novos)} promoção(ões) nova(s)** com -{DESCONTO_MINIMO}% ou mais!\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    # Enviar embed de resumo se houver muitos jogos
    if len(novos) > 3:
        embed_resumo = criar_embed_resumo(novos)
        await canal.send(embed=embed_resumo)
        await asyncio.sleep(1)

    # Enviar embeds individuais (máx 10 para não spammar)
    for jogo in novos[:10]:
        embed = criar_embed_jogo(jogo)
        await canal.send(embed=embed)
        jogos_enviados.add(jogo["appid"])
        await asyncio.sleep(0.8)

    if len(novos) > 10:
        await canal.send(
            f"📋 *...e mais {len(novos) - 10} promoções! Use `!promocoes` para ver todas.*"
        )

    print(f"[INFO] {min(len(novos), 10)} promoção(ões) enviada(s) para #{canal.name}")


# ─── Comandos ─────────────────────────────────────────────────────────────────
@bot.command(name="promocoes", aliases=["promo", "sales", "desconto"])
async def cmd_promocoes(ctx, desconto: int = None):
    """Busca e exibe as promoções atuais da Steam."""
    desc = desconto if desconto is not None else DESCONTO_MINIMO

    if desc < 1 or desc > 99:
        await ctx.send("❌ O desconto deve ser entre 1% e 99%.")
        return

    msg_espera = await ctx.send(f"🔍 Buscando promoções na Steam com -{desc}% ou mais... aguarde!")

    jogos = await buscar_promocoes_steam(desc)

    await msg_espera.delete()

    if not jogos:
        await ctx.send(
            f"😔 Nenhuma promoção encontrada com -{desc}% ou mais no momento.\n"
            f"Tente um percentual menor: `!promocoes {max(1, desc - 20)}`"
        )
        return

    await ctx.send(
        f"🎮 **{len(jogos)} promoção(ões) encontrada(s) com -{desc}% ou mais!**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    if len(jogos) > 3:
        embed_resumo = criar_embed_resumo(jogos)
        await ctx.send(embed=embed_resumo)

    for jogo in jogos[:5]:
        embed = criar_embed_jogo(jogo)
        await ctx.send(embed=embed)
        await asyncio.sleep(0.5)

    if len(jogos) > 5:
        await ctx.send(f"📋 *Mostrando 5 de {len(jogos)}. Use `!promocoes {desc}` novamente para atualizar.*")


@bot.command(name="ajuda", aliases=["help", "comandos"])
async def cmd_ajuda(ctx):
    """Lista os comandos disponíveis."""
    embed = discord.Embed(
        title="🤖 Comandos do Bot de Promoções Steam",
        description="Seu assistente de promoções da Steam em BRL 🇧🇷",
        color=discord.Color.from_rgb(23, 153, 232)
    )
    embed.add_field(
        name="🔍 `!promocoes [desconto]`",
        value="Busca promoções da Steam agora.\n"
              "Ex: `!promocoes` → desconto padrão\n"
              "Ex: `!promocoes 70` → apenas -70% ou mais",
        inline=False
    )
    embed.add_field(
        name="⚙️ `!config`",
        value="Exibe as configurações atuais do bot.",
        inline=False
    )
    embed.add_field(
        name="🔄 `!atualizar`",
        value="Força uma atualização imediata das promoções no canal configurado. (Admin)",
        inline=False
    )
    embed.add_field(
        name="📋 `!ajuda`",
        value="Exibe esta mensagem.",
        inline=False
    )
    embed.set_footer(text="Steam BR Bot • Preços em R$ (BRL)")
    await ctx.send(embed=embed)


@bot.command(name="config")
async def cmd_config(ctx):
    """Exibe as configurações atuais do bot."""
    canal = bot.get_channel(CANAL_ID)
    canal_nome = f"#{canal.name}" if canal else "❌ Não configurado"

    proxima = verificar_promocoes.next_iteration
    proxima_str = proxima.strftime("%d/%m/%Y às %H:%M UTC") if proxima else "Não agendado"

    embed = discord.Embed(
        title="⚙️ Configurações do Bot",
        color=discord.Color.blurple()
    )
    embed.add_field(name="📢 Canal de Promoções", value=canal_nome, inline=True)
    embed.add_field(name="📉 Desconto Mínimo", value=f"-{DESCONTO_MINIMO}%", inline=True)
    embed.add_field(name="⏱️ Intervalo de Verificação", value=f"A cada {INTERVALO_HORAS}h", inline=True)
    embed.add_field(name="🔄 Próxima Verificação", value=proxima_str, inline=False)
    embed.add_field(name="💾 Jogos em Cache (sessão)", value=str(len(jogos_enviados)), inline=True)
    embed.set_footer(text="Steam BR Bot • Configure via variáveis de ambiente (.env)")
    await ctx.send(embed=embed)


@bot.command(name="atualizar")
@commands.has_permissions(administrator=True)
async def cmd_atualizar(ctx):
    """Força uma verificação imediata (Admin)."""
    await ctx.send("🔄 Forçando verificação de promoções agora...")
    await verificar_promocoes()
    await ctx.send("✅ Verificação concluída!")


@cmd_atualizar.error
async def atualizar_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Apenas administradores podem usar este comando.")


# ─── Eventos ──────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ Bot conectado como {bot.user} (ID: {bot.user.id})")
    print(f"   Canal configurado: {CANAL_ID}")
    print(f"   Desconto mínimo: {DESCONTO_MINIMO}%")
    print(f"   Verificação a cada: {INTERVALO_HORAS}h")

    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="promoções da Steam 🎮"
        )
    )

    if not verificar_promocoes.is_running():
        verificar_promocoes.start()
        print("   Task de promoções iniciada!")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.send(f"❓ Comando desconhecido. Use `!ajuda` para ver os comandos disponíveis.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠️ Argumento faltando. Use `!ajuda` para ver como usar o comando.")


# ─── Iniciar Bot ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("❌ ERRO: DISCORD_TOKEN não definido no arquivo .env!")
        exit(1)
    if CANAL_ID == 0:
        print("⚠️  AVISO: CANAL_ID não definido. Use !promocoes manualmente ou configure o .env")
    bot.run(DISCORD_TOKEN)
