import discord
from discord.ext import commands, tasks
from discord.ui import View, Button
import aiohttp
import asyncio
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ─── Configurações ───────────────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CANAL_ID = int(os.getenv("CANAL_ID", "0"))
DESCONTO_MINIMO = int(os.getenv("DESCONTO_MINIMO", "50"))
INTERVALO_HORAS = int(os.getenv("INTERVALO_HORAS", "6"))
NOTA_MINIMA = int(os.getenv("NOTA_MINIMA", "70"))       # % mínimo de avaliações positivas
EXCLUIR_INDIE = os.getenv("EXCLUIR_INDIE", "true").lower() == "true"
JOGOS_POR_PAGINA = 10

jogos_enviados = set()

# ─── Bot Setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

STEAM_SEARCH_URL = "https://store.steampowered.com/search/results/"
STEAM_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
STEAM_REVIEWS_URL = "https://store.steampowered.com/appreviews/{appid}"
STEAM_APP_URL = "https://store.steampowered.com/app/{appid}"

# categoria Steam para "Indie" = category id 492 (genre) — vamos checar pelas genres do appdetails
GENRE_INDIE = "Indie"


# ─── Busca de promoções (com paginação real da Steam) ────────────────────────
async def buscar_pagina_busca(session: aiohttp.ClientSession, start: int, count: int = 50) -> list[dict]:
    """Busca uma página de resultados de promoções direto do endpoint de busca da Steam."""
    params = {
        "start": start,
        "count": count,
        "specials": 1,          # apenas jogos em promoção
        "cc": "br",
        "l": "portuguese",
        "category1": 998,       # 998 = Jogos (exclui software, DLC solto etc.)
        "json": 1,               # ESSENCIAL: sem isso a Steam devolve a página HTML, não o JSON
    }
    try:
        async with session.get(STEAM_SEARCH_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                print(f"[AVISO] Steam retornou status {resp.status} na página start={start}")
                return []
            data = await resp.json(content_type=None)
            items = data.get("items", []) if isinstance(data, dict) else []
            print(f"[INFO] Página start={start}: {len(items)} jogos recebidos")
            return items
    except Exception as e:
        print(f"[ERRO] Falha ao buscar página start={start}: {e}")
        return []


async def buscar_detalhes_jogo(session: aiohttp.ClientSession, appid: int) -> dict | None:
    """Busca detalhes completos (gêneros, preço, nota, imagem) de um jogo específico."""
    params = {"appids": appid, "cc": "br", "l": "portuguese",
              "filters": "price_overview,genres,name,type,header_image"}
    try:
        async with session.get(STEAM_APPDETAILS_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            entry = data.get(str(appid))
            if not entry or not entry.get("success"):
                return None
            return entry.get("data")
    except Exception:
        return None


async def buscar_avaliacao(session: aiohttp.ClientSession, appid: int) -> dict | None:
    """Busca a nota de avaliação (review score) de um jogo."""
    params = {"json": 1, "language": "all", "purchase_type": "all", "num_per_page": 0}
    try:
        async with session.get(
            STEAM_REVIEWS_URL.format(appid=appid), params=params, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            summary = data.get("query_summary", {})
            total = summary.get("total_reviews", 0)
            positivas = summary.get("total_positive", 0)
            if total == 0:
                return {"percentual": 0, "total": 0, "descricao": "Sem avaliações"}
            percentual = round((positivas / total) * 100)
            return {
                "percentual": percentual,
                "total": total,
                "descricao": summary.get("review_score_desc", ""),
            }
    except Exception:
        return None


async def processar_jogo(session: aiohttp.ClientSession, item: dict, desconto_minimo: int,
                          nota_minima: int, excluir_indie: bool) -> dict | None:
    """Processa um item bruto da busca: pega detalhes + avaliação, aplica filtros."""
    appid = item.get("id")
    if not appid:
        return None

    discount = item.get("discount_percent", 0)
    if discount < desconto_minimo:
        return None

    # Detalhes (gêneros + confirmação de preço)
    detalhes = await buscar_detalhes_jogo(session, appid)
    if not detalhes:
        return None

    # Excluir DLCs, trilhas sonoras etc — só jogos completos
    if detalhes.get("type") != "game":
        return None

    # Filtro indie
    generos = [g.get("description", "") for g in detalhes.get("genres", [])]
    if excluir_indie and GENRE_INDIE in generos:
        return None

    # Avaliação
    avaliacao = await buscar_avaliacao(session, appid)
    if not avaliacao or avaliacao["total"] < 10:  # ignora jogos com poucas avaliações (dados não confiáveis)
        return None
    if avaliacao["percentual"] < nota_minima:
        return None

    preco_info = detalhes.get("price_overview", {})
    preco_original = preco_info.get("initial", 0) / 100
    preco_final = preco_info.get("final", 0) / 100

    return {
        "appid": appid,
        "nome": detalhes.get("name", item.get("name", "Desconhecido")),
        "desconto": discount,
        "preco_original": preco_original,
        "preco_final": preco_final,
        "imagem": detalhes.get("header_image", ""),
        "url": STEAM_APP_URL.format(appid=appid),
        "generos": generos,
        "avaliacao_percentual": avaliacao["percentual"],
        "avaliacao_total": avaliacao["total"],
        "avaliacao_desc": avaliacao["descricao"],
    }


async def buscar_promocoes_steam(
    desconto_minimo: int = 50,
    nota_minima: int = 70,
    excluir_indie: bool = True,
    max_paginas: int = 6,
    max_resultados: int = 60,
) -> list[dict]:
    """
    Busca promoções na Steam, paginando pelo endpoint de busca real (não só 'featured'),
    aplicando filtro de desconto, nota de avaliação e exclusão de indies.
    """
    resultados_brutos = []

    async with aiohttp.ClientSession(headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Referer": "https://store.steampowered.com/search/?specials=1",
    }) as session:
        # 1. Paginar pelo endpoint de busca para pegar MUITO mais jogos em promoção
        for pagina in range(max_paginas):
            start = pagina * 50
            items = await buscar_pagina_busca(session, start=start, count=50)
            if not items:
                break
            resultados_brutos.extend(items)
            if len(resultados_brutos) >= 300:  # limite de segurança
                break
            await asyncio.sleep(0.3)  # não martelar a API da Steam

        if not resultados_brutos:
            return []

        # 2. Processar cada jogo em paralelo (com limite de concorrência)
        semaforo = asyncio.Semaphore(8)

        async def processar_com_limite(item):
            async with semaforo:
                return await processar_jogo(session, item, desconto_minimo, nota_minima, excluir_indie)

        tarefas = [processar_com_limite(item) for item in resultados_brutos]
        processados = await asyncio.gather(*tarefas)

    jogos_validos = [j for j in processados if j is not None]

    # Remover duplicatas
    vistos = set()
    resultado_final = []
    for jogo in jogos_validos:
        if jogo["appid"] not in vistos:
            vistos.add(jogo["appid"])
            resultado_final.append(jogo)

    # Ordenar por: nota de avaliação (desc), depois por desconto (desc)
    resultado_final.sort(key=lambda x: (x["avaliacao_percentual"], x["desconto"]), reverse=True)

    return resultado_final[:max_resultados]


# ─── Formatação ───────────────────────────────────────────────────────────────
def formatar_real(valor: float) -> str:
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def cor_desconto(desconto: int) -> discord.Color:
    if desconto >= 80:
        return discord.Color.gold()
    elif desconto >= 60:
        return discord.Color.green()
    elif desconto >= 40:
        return discord.Color.teal()
    return discord.Color.blue()


def emoji_desconto(desconto: int) -> str:
    if desconto >= 90: return "🔥🔥🔥"
    if desconto >= 75: return "🔥🔥"
    if desconto >= 50: return "🔥"
    if desconto >= 25: return "💸"
    return "🏷️"


def emoji_avaliacao(percentual: int) -> str:
    if percentual >= 95: return "🌟"
    if percentual >= 85: return "⭐"
    if percentual >= 70: return "👍"
    return "🙂"


def criar_embed_jogo(jogo: dict, indice: int, total: int) -> discord.Embed:
    desconto = jogo["desconto"]
    embed = discord.Embed(
        title=f"{emoji_desconto(desconto)} {jogo['nome']}",
        url=jogo["url"],
        color=cor_desconto(desconto),
        timestamp=datetime.utcnow(),
    )

    if jogo["preco_original"] > 0:
        embed.add_field(name="💰 Preço Original", value=f"~~{formatar_real(jogo['preco_original'])}~~", inline=True)
        embed.add_field(name="✅ Com Desconto", value=f"**{formatar_real(jogo['preco_final'])}**", inline=True)
        economia = jogo["preco_original"] - jogo["preco_final"]
        embed.add_field(name="📉 Desconto", value=f"**-{desconto}%**\n(economia {formatar_real(economia)})", inline=True)
    else:
        embed.add_field(name="💰 Preço", value="**GRÁTIS!** 🎉", inline=False)

    emoji_nota = emoji_avaliacao(jogo["avaliacao_percentual"])
    embed.add_field(
        name=f"{emoji_nota} Avaliação dos Jogadores",
        value=f"**{jogo['avaliacao_percentual']}% positivas** ({jogo['avaliacao_total']:,} avaliações)".replace(",", "."),
        inline=False,
    )

    if jogo.get("generos"):
        embed.add_field(name="🏷️ Gêneros", value=", ".join(jogo["generos"][:4]), inline=False)

    if jogo.get("imagem"):
        embed.set_image(url=jogo["imagem"])

    embed.set_footer(text=f"Steam BR • Jogo {indice} de {total} • Página")
    return embed


# ─── View de paginação (botões) ───────────────────────────────────────────────
class PaginacaoView(View):
    """View com botões para navegar entre os jogos em promoção, um por vez."""

    def __init__(self, jogos: list[dict], autor_id: int, pagina_atual: int = 0):
        super().__init__(timeout=180)  # expira após 3 min de inatividade
        self.jogos = jogos
        self.autor_id = autor_id
        self.pagina_atual = pagina_atual
        self._atualizar_botoes()

    def _atualizar_botoes(self):
        self.anterior.disabled = self.pagina_atual == 0
        self.proximo.disabled = self.pagina_atual >= len(self.jogos) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.autor_id:
            await interaction.response.send_message(
                "❌ Apenas quem pediu a busca pode navegar. Use `!promocoes` para fazer a sua própria busca!",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="◀ Anterior", style=discord.ButtonStyle.secondary)
    async def anterior(self, interaction: discord.Interaction, button: Button):
        self.pagina_atual = max(0, self.pagina_atual - 1)
        self._atualizar_botoes()
        embed = criar_embed_jogo(self.jogos[self.pagina_atual], self.pagina_atual + 1, len(self.jogos))
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Próximo ▶", style=discord.ButtonStyle.primary)
    async def proximo(self, interaction: discord.Interaction, button: Button):
        self.pagina_atual = min(len(self.jogos) - 1, self.pagina_atual + 1)
        self._atualizar_botoes()
        embed = criar_embed_jogo(self.jogos[self.pagina_atual], self.pagina_atual + 1, len(self.jogos))
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="🔢 Ir para...", style=discord.ButtonStyle.secondary)
    async def ir_para(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            f"Digite `!ir <número>` (1 a {len(self.jogos)}) para pular direto para um jogo.",
            ephemeral=True,
        )


def criar_embed_resumo(jogos: list[dict]) -> discord.Embed:
    embed = discord.Embed(
        title="🎮 Promoções da Steam — Lista Completa",
        description=(
            f"**{len(jogos)} jogos** encontrados (bem avaliados, sem indies)\n"
            f"Ordenados por **nota de avaliação** e depois por **desconto**"
        ),
        color=discord.Color.from_rgb(23, 153, 232),
        timestamp=datetime.utcnow(),
    )

    for i, jogo in enumerate(jogos[:JOGOS_POR_PAGINA], start=1):
        preco_txt = formatar_real(jogo["preco_final"]) if jogo["preco_final"] > 0 else "GRÁTIS"
        embed.add_field(
            name=f"{i}. {emoji_desconto(jogo['desconto'])} {jogo['nome'][:32]}",
            value=(
                f"-{jogo['desconto']}% → **{preco_txt}**\n"
                f"{emoji_avaliacao(jogo['avaliacao_percentual'])} {jogo['avaliacao_percentual']}% positivas\n"
                f"[Ver na Steam]({jogo['url']})"
            ),
            inline=True,
        )

    embed.set_footer(text=f"Steam BR • {len(jogos)} jogos no total • Use os botões abaixo para navegar um por um")
    return embed


# ─── Task automática ──────────────────────────────────────────────────────────
@tasks.loop(hours=INTERVALO_HORAS)
async def verificar_promocoes():
    await bot.wait_until_ready()
    canal = bot.get_channel(CANAL_ID)
    if not canal:
        print(f"[AVISO] Canal {CANAL_ID} não encontrado.")
        return

    print(f"[INFO] Buscando promoções (desconto>={DESCONTO_MINIMO}%, nota>={NOTA_MINIMA}%, indie excluído={EXCLUIR_INDIE})...")
    jogos = await buscar_promocoes_steam(DESCONTO_MINIMO, NOTA_MINIMA, EXCLUIR_INDIE)

    if not jogos:
        print("[INFO] Nenhuma promoção encontrada.")
        return

    novos = [j for j in jogos if j["appid"] not in jogos_enviados]
    if not novos:
        print("[INFO] Nenhuma promoção nova.")
        return

    horario = datetime.now().strftime("%d/%m/%Y às %H:%M")
    await canal.send(
        f"🕹️ **Atualização de promoções — {horario}**\n"
        f"**{len(novos)} promoção(ões) nova(s)** bem avaliadas e sem indies!\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    embed_resumo = criar_embed_resumo(novos)
    view = PaginacaoView(novos, autor_id=bot.user.id)
    await canal.send(embed=embed_resumo)
    if novos:
        primeiro_embed = criar_embed_jogo(novos[0], 1, len(novos))
        await canal.send(embed=primeiro_embed, view=view)

    for jogo in novos:
        jogos_enviados.add(jogo["appid"])

    print(f"[INFO] {len(novos)} promoção(ões) processada(s) para #{canal.name}")


# ─── Comandos ─────────────────────────────────────────────────────────────────
# Guarda a última busca de cada usuário para o comando !ir funcionar
ultima_busca: dict[int, list[dict]] = {}


@bot.command(name="promocoes", aliases=["promo", "sales", "desconto"])
async def cmd_promocoes(ctx, desconto: int = None):
    """Busca promoções atuais da Steam: bem avaliadas, sem indies, com paginação."""
    desc = desconto if desconto is not None else DESCONTO_MINIMO
    if desc < 1 or desc > 99:
        await ctx.send("❌ O desconto deve ser entre 1% e 99%.")
        return

    msg_espera = await ctx.send(
        f"🔍 Buscando promoções na Steam com -{desc}% ou mais, nota ≥{NOTA_MINIMA}%, sem indies...\n"
        f"⏳ Isso pode levar até 30 segundos (estamos vasculhando várias páginas!)"
    )

    jogos = await buscar_promocoes_steam(desc, NOTA_MINIMA, EXCLUIR_INDIE)
    await msg_espera.delete()

    if not jogos:
        await ctx.send(
            f"😔 Nenhuma promoção encontrada com esses critérios.\n"
            f"Tente: `!promocoes {max(1, desc - 20)}` ou `!notaminima 50` para afrouxar o filtro de avaliação."
        )
        return

    ultima_busca[ctx.author.id] = jogos

    await ctx.send(
        f"🎮 **{len(jogos)} promoção(ões) encontrada(s)!** (bem avaliados, sem indies)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    embed_resumo = criar_embed_resumo(jogos)
    await ctx.send(embed=embed_resumo)

    view = PaginacaoView(jogos, autor_id=ctx.author.id)
    embed_primeiro = criar_embed_jogo(jogos[0], 1, len(jogos))
    await ctx.send(embed=embed_primeiro, view=view)


@bot.command(name="ir")
async def cmd_ir(ctx, numero: int):
    """Pula direto para um jogo específico da última busca feita."""
    jogos = ultima_busca.get(ctx.author.id)
    if not jogos:
        await ctx.send("❌ Você ainda não fez nenhuma busca. Use `!promocoes` primeiro.")
        return
    if numero < 1 or numero > len(jogos):
        await ctx.send(f"❌ Escolha um número entre 1 e {len(jogos)}.")
        return

    view = PaginacaoView(jogos, autor_id=ctx.author.id, pagina_atual=numero - 1)
    embed = criar_embed_jogo(jogos[numero - 1], numero, len(jogos))
    await ctx.send(embed=embed, view=view)


@bot.command(name="notaminima")
async def cmd_notaminima(ctx, valor: int = None):
    """Exibe ou altera a nota mínima de avaliação (só nesta sessão)."""
    global NOTA_MINIMA
    try:
        if valor is None:
            await ctx.send(f"⭐ Nota mínima atual: **{NOTA_MINIMA}%** de avaliações positivas.")
            return
        if valor < 0 or valor > 100:
            await ctx.send("❌ A nota deve ser entre 0 e 100.")
            return
        NOTA_MINIMA = valor
        await ctx.send(f"✅ Nota mínima ajustada para **{valor}%**. Use `!promocoes` para buscar novamente.")
    except Exception as e:
        await ctx.send(f"❌ Erro ao processar o comando: `{e}`")


@bot.command(name="debug")
async def cmd_debug(ctx):
    """Testa cada etapa da busca isoladamente para descobrir onde está travando."""
    msg = await ctx.send("🔧 Rodando diagnóstico, aguarde...")
    linhas = []

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Referer": "https://store.steampowered.com/search/?specials=1",
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        # 1. Testar endpoint de busca (specials)
        try:
            params = {
                "start": 0,
                "count": 10,
                "specials": 1,
                "cc": "br",
                "l": "portuguese",
                "category1": 998,
                "json": 1,
            }
            async with session.get(STEAM_SEARCH_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                texto = await resp.text()
                linhas.append(f"**1. Busca de promoções** → status HTTP `{resp.status}`")
                if resp.status == 200:
                    try:
                        data = await resp.json(content_type=None)
                        items = data.get("items", [])
                        total_count = data.get("total_count", "?")
                        linhas.append(f"   ✅ JSON válido, `{len(items)}` jogos recebidos (total_count: `{total_count}`)")
                        if items:
                            linhas.append(f"   Exemplo: `{items[0].get('name', '???')}` (appid {items[0].get('id')})")
                        else:
                            linhas.append(f"   Corpo bruto (primeiros 300 chars): `{texto[:300]}`")
                    except Exception as e:
                        linhas.append(f"   ❌ Resposta não é JSON válido: {e}")
                        linhas.append(f"   Início da resposta: `{texto[:150]}`")
                else:
                    linhas.append(f"   ❌ Corpo da resposta: `{texto[:200]}`")
        except Exception as e:
            linhas.append(f"**1. Busca de promoções** → ❌ Exceção: `{e}`")

        # 2. Testar appdetails com um appid conhecido (Counter-Strike 2 = 730)
        try:
            params2 = {"appids": 730, "cc": "br", "l": "portuguese", "filters": "price_overview,genres,name,type"}
            async with session.get(STEAM_APPDETAILS_URL, params=params2, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                linhas.append(f"\n**2. Detalhes de jogo (appdetails)** → status HTTP `{resp.status}`")
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    entry = data.get("730", {})
                    linhas.append(f"   success: `{entry.get('success')}`")
                else:
                    texto2 = await resp.text()
                    linhas.append(f"   ❌ Corpo: `{texto2[:200]}`")
        except Exception as e:
            linhas.append(f"\n**2. Detalhes de jogo** → ❌ Exceção: `{e}`")

        # 3. Testar avaliações do mesmo jogo
        try:
            params3 = {"json": 1, "language": "all", "purchase_type": "all", "num_per_page": 0}
            async with session.get(STEAM_REVIEWS_URL.format(appid=730), params=params3, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                linhas.append(f"\n**3. Avaliações (appreviews)** → status HTTP `{resp.status}`")
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    summary = data.get("query_summary", {})
                    linhas.append(f"   total_reviews: `{summary.get('total_reviews')}`")
        except Exception as e:
            linhas.append(f"\n**3. Avaliações** → ❌ Exceção: `{e}`")

    embed = discord.Embed(
        title="🔧 Diagnóstico da API da Steam",
        description="\n".join(linhas)[:4000],
        color=discord.Color.orange(),
    )
    await msg.delete()
    await ctx.send(embed=embed)



async def cmd_ajuda(ctx):
    embed = discord.Embed(
        title="🤖 Comandos do Bot de Promoções Steam",
        description="Seu assistente de promoções da Steam em BRL 🇧🇷\nFiltra jogos indie e prioriza os bem avaliados!",
        color=discord.Color.from_rgb(23, 153, 232),
    )
    embed.add_field(
        name="🔍 `!promocoes [desconto]`",
        value="Busca promoções (padrão -50% ou o valor que você passar).\n"
              "Mostra uma lista + navegação com botões ◀ ▶",
        inline=False,
    )
    embed.add_field(
        name="🔢 `!ir <número>`",
        value="Pula direto para o jogo N da sua última busca.",
        inline=False,
    )
    embed.add_field(
        name="⭐ `!notaminima [valor]`",
        value="Vê ou ajusta a nota mínima de avaliação exigida (padrão 70%).",
        inline=False,
    )
    embed.add_field(name="⚙️ `!config`", value="Mostra as configurações atuais.", inline=False)
    embed.add_field(name="🔄 `!atualizar`", value="Força atualização no canal automático. (Admin)", inline=False)
    embed.set_footer(text="Steam BR Bot • Preços em R$ (BRL) • Sem indies, só jogos bem avaliados")
    await ctx.send(embed=embed)


@bot.command(name="config")
async def cmd_config(ctx):
    canal = bot.get_channel(CANAL_ID)
    canal_nome = f"#{canal.name}" if canal else "❌ Não configurado"
    proxima = verificar_promocoes.next_iteration
    proxima_str = proxima.strftime("%d/%m/%Y às %H:%M UTC") if proxima else "Não agendado"

    embed = discord.Embed(title="⚙️ Configurações do Bot", color=discord.Color.blurple())
    embed.add_field(name="📢 Canal", value=canal_nome, inline=True)
    embed.add_field(name="📉 Desconto Mínimo", value=f"-{DESCONTO_MINIMO}%", inline=True)
    embed.add_field(name="⭐ Nota Mínima", value=f"{NOTA_MINIMA}%", inline=True)
    embed.add_field(name="🚫 Excluir Indies", value="Sim" if EXCLUIR_INDIE else "Não", inline=True)
    embed.add_field(name="⏱️ Intervalo", value=f"a cada {INTERVALO_HORAS}h", inline=True)
    embed.add_field(name="🔄 Próxima Verificação", value=proxima_str, inline=False)
    await ctx.send(embed=embed)


@bot.command(name="atualizar")
@commands.has_permissions(administrator=True)
async def cmd_atualizar(ctx):
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
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="promoções da Steam 🎮")
    )
    if not verificar_promocoes.is_running():
        verificar_promocoes.start()
        print("   Task de promoções iniciada!")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.send("❓ Comando desconhecido. Use `!ajuda` para ver os comandos disponíveis.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("⚠️ Argumento faltando. Use `!ajuda` para ver como usar o comando.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("⚠️ Argumento inválido — verifique se digitou um número corretamente.")


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("❌ ERRO: DISCORD_TOKEN não definido no arquivo .env!")
        exit(1)
    if CANAL_ID == 0:
        print("⚠️  AVISO: CANAL_ID não definido. Use !promocoes manualmente ou configure o .env")
    bot.run(DISCORD_TOKEN)
