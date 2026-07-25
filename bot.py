import discord
from discord.ext import commands, tasks
from discord.ui import View, Button
import aiohttp
import asyncio
import os
import unicodedata
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
ITAD_API_KEY = os.getenv("ITAD_API_KEY", "")            # chave gratuita: isthereanydeal.com/apps/my/
JOGOS_POR_PAGINA = 10
RESULTADOS_POR_PAGINA = 25  # A Steam limita esse endpoint a 25 itens por requisição.
MAX_CONCORRENCIA_STEAM = 8

jogos_enviados = set()

# ─── Bot Setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

STEAM_SEARCH_URL = "https://store.steampowered.com/search/results/"
STEAM_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
STEAM_REVIEWS_URL = "https://store.steampowered.com/appreviews/{appid}"
STEAM_APP_URL = "https://store.steampowered.com/app/{appid}"

ITAD_LOOKUP_URL = "https://api.isthereanydeal.com/games/lookup/v1"
ITAD_PRICES_URL = "https://api.isthereanydeal.com/games/prices/v3"

# categoria Steam para "Indie" = category id 492 (genre) — vamos checar pelas genres do appdetails
GENRE_INDIE = "Indie"


# ─── Busca de promoções (com paginação real da Steam) ────────────────────────
def extrair_appid(item: dict) -> int | None:
    """Obtém o appid tanto das respostas novas quanto das respostas antigas da Steam."""
    appid = item.get("id") or item.get("appid")
    if appid:
        try:
            return int(appid)
        except (TypeError, ValueError):
            return None

    logo = item.get("logo", "")
    import re
    match = re.search(r"/(?:apps|subs)/(\d+)/", logo)
    return int(match.group(1)) if match else None


def normalizar_texto(valor: str) -> str:
    """Normaliza nomes para comparar buscas sem diferenças de acento ou caixa."""
    sem_acento = unicodedata.normalize("NFKD", valor).encode("ASCII", "ignore").decode("ASCII")
    return " ".join(sem_acento.casefold().split())


async def buscar_pagina_busca(
    session: aiohttp.ClientSession,
    start: int,
    count: int = RESULTADOS_POR_PAGINA,
    termo: str | None = None,
    apenas_promocoes: bool = True,
) -> list[dict]:
    """Busca uma página de resultados de promoções direto do endpoint de busca da Steam."""
    params = {
        "start": start,
        "count": min(count, RESULTADOS_POR_PAGINA),
        "cc": "br",
        "l": "portuguese",
        "category1": 998,       # 998 = Jogos (exclui software, DLC solto etc.)
        "json": 1,               # ESSENCIAL: sem isso a Steam devolve a página HTML, não o JSON
    }
    if apenas_promocoes:
        params["specials"] = 1
    if termo:
        params["term"] = termo
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
    # Sem 'filters' — esse parâmetro às vezes faz a Steam omitir até campos básicos como 'type'.
    # Pegar a resposta completa é mais confiável (um pouco mais pesado, mas garante os campos certos).
    params = {"appids": appid, "cc": "br", "l": "portuguese"}
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


# ─── IsThereAnyDeal (ITAD) — preço mínimo histórico ───────────────────────────
# API oficial e gratuita: https://docs.isthereanydeal.com/
# Chave grátis em: https://isthereanydeal.com/apps/my/
async def buscar_itad_game_id(session: aiohttp.ClientSession, appid: int) -> str | None:
    """Traduz um Steam appid para o ID interno do ITAD."""
    if not ITAD_API_KEY:
        return None
    params = {"key": ITAD_API_KEY, "appid": appid}
    try:
        async with session.get(ITAD_LOOKUP_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            if not data.get("found"):
                return None
            return data.get("game", {}).get("id")
    except Exception:
        return None


async def buscar_preco_minimo_historico(session: aiohttp.ClientSession, itad_game_id: str) -> dict | None:
    """Busca o preço mínimo histórico (all-time) em BRL e a data da promoção que gerou esse preço."""
    if not ITAD_API_KEY or not itad_game_id:
        return None
    params = {"key": ITAD_API_KEY, "country": "BR"}
    try:
        async with session.post(
            ITAD_PRICES_URL, params=params, json=[itad_game_id], timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            if not data:
                return None
            entry = data[0]
            history_low = entry.get("historyLow", {}).get("all")
            if not history_low:
                return None

            # O endpoint de preços não traz a data exata do menor histórico —
            # usamos a data do deal atual da Steam como referência mais próxima disponível.
            data_promocao = None
            for deal in entry.get("deals", []):
                if deal.get("shop", {}).get("name") == "Steam":
                    data_promocao = deal.get("timestamp")
                    break

            return {
                "preco_minimo": history_low.get("amount", 0),
                "data_promocao": data_promocao,
            }
    except Exception:
        return None


async def processar_jogo_debug(session: aiohttp.ClientSession, item: dict) -> str:
    """Versão instrumentada de processar_jogo só para diagnóstico: retorna uma string dizendo onde parou."""
    appid = item.get("id") or item.get("appid")
    if not appid and item.get("logo"):
        import re
        m = re.search(r"/apps/(\d+)/", item.get("logo", ""))
        if m:
            appid = int(m.group(1))
    if not appid:
        return "❌ Parou em: extração do appid (não achou nem em id/appid/logo)"

    detalhes = await buscar_detalhes_jogo(session, appid)
    if not detalhes:
        return f"❌ Parou em: buscar_detalhes_jogo(appid={appid}) retornou None (appdetails falhou ou success=False)"

    tipo = detalhes.get("type")
    if tipo is not None and tipo != "game":
        return f"❌ Parou em: filtro de tipo — type='{tipo}' (esperado 'game') appid={appid}"

    preco_info = detalhes.get("price_overview")
    if not preco_info:
        return f"❌ Parou em: price_overview ausente/vazio — chaves do detalhes: {list(detalhes.keys())}"

    discount = preco_info.get("discount_percent", 0)

    avaliacao = await buscar_avaliacao(session, appid)
    if not avaliacao:
        return f"❌ Parou em: buscar_avaliacao(appid={appid}) retornou None"
    if avaliacao["total"] < 10:
        return f"⚠️ Parou em: menos de 10 avaliações (total={avaliacao['total']})"

    return (
        f"✅ Chegou ao fim! nome={detalhes.get('name')} desconto={discount}% "
        f"nota={avaliacao['percentual']}% total_reviews={avaliacao['total']}"
    )


async def montar_jogo(session: aiohttp.ClientSession, item: dict) -> dict | None:
    """Converte um resultado da busca em dados completos de um jogo da Steam."""
    appid = extrair_appid(item)
    if not appid:
        return None

    detalhes = await buscar_detalhes_jogo(session, appid)
    if not detalhes or detalhes.get("type") not in (None, "game"):
        return None

    preco_info = detalhes.get("price_overview") or {}
    avaliacao = await buscar_avaliacao(session, appid)
    if not avaliacao:
        avaliacao = {"percentual": 0, "total": 0, "descricao": "Sem avaliações"}

    return {
        "appid": appid,
        "nome": detalhes.get("name", item.get("name", "Desconhecido")),
        "desconto": preco_info.get("discount_percent", 0),
        "preco_original": preco_info.get("initial", 0) / 100,
        "preco_final": preco_info.get("final", 0) / 100,
        "preco_minimo_historico": None,
        "data_promocao_historica": None,
        "imagem": detalhes.get("header_image", ""),
        "url": STEAM_APP_URL.format(appid=appid),
        "generos": [g.get("description", "") for g in detalhes.get("genres", [])],
        "avaliacao_percentual": avaliacao["percentual"],
        "avaliacao_total": avaliacao["total"],
        "avaliacao_desc": avaliacao["descricao"],
    }


async def adicionar_historico(session: aiohttp.ClientSession, jogo: dict) -> None:
    """Acrescenta o menor preço do ITAD somente aos resultados que serão exibidos."""
    if not ITAD_API_KEY:
        return
    itad_id = await buscar_itad_game_id(session, jogo["appid"])
    if not itad_id:
        return
    historico = await buscar_preco_minimo_historico(session, itad_id)
    if historico:
        jogo["preco_minimo_historico"] = historico.get("preco_minimo")
        jogo["data_promocao_historica"] = historico.get("data_promocao")


async def processar_jogo(session: aiohttp.ClientSession, item: dict, desconto_minimo: int,
                          nota_minima: int, excluir_indie: bool) -> dict | None:
    """Processa e filtra um jogo sem fazer chamadas extras ao ITAD para descartados."""
    jogo = await montar_jogo(session, item)
    if not jogo:
        return None
    if jogo["desconto"] < desconto_minimo:
        return None
    if excluir_indie and GENRE_INDIE in jogo["generos"]:
        return None
    if jogo["avaliacao_total"] < 10 or jogo["avaliacao_percentual"] < nota_minima:
        return None
    return jogo


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
            start = pagina * RESULTADOS_POR_PAGINA
            items = await buscar_pagina_busca(session, start=start)
            if not items:
                break
            resultados_brutos.extend(items)
            if len(resultados_brutos) >= max_paginas * RESULTADOS_POR_PAGINA:
                break
            await asyncio.sleep(0.3)  # não martelar a API da Steam

        if not resultados_brutos:
            return []

        # 2. A Steam limita a busca a 25 itens. Não pule páginas e não faça
        # centenas de requisições simultâneas, pois isso costuma gerar 429.
        itens_unicos = []
        appids_vistos = set()
        for item in resultados_brutos:
            appid = extrair_appid(item)
            if appid and appid not in appids_vistos:
                appids_vistos.add(appid)
                itens_unicos.append(item)

        # 3. Processar cada jogo em paralelo, respeitando um limite seguro.
        semaforo = asyncio.Semaphore(MAX_CONCORRENCIA_STEAM)

        async def processar_com_limite(item):
            async with semaforo:
                return await processar_jogo(session, item, desconto_minimo, nota_minima, excluir_indie)

        tarefas = [processar_com_limite(item) for item in itens_unicos]
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

    resultado_final = resultado_final[:max_resultados]

    # O histórico é opcional e caro: consulte-o apenas para jogos aprovados,
    # nunca para todos os candidatos que serão descartados pelos filtros.
    if ITAD_API_KEY:
        semaforo_itad = asyncio.Semaphore(3)

        async def enriquecer_com_limite(itad_session: aiohttp.ClientSession, jogo: dict) -> None:
            async with semaforo_itad:
                await adicionar_historico(itad_session, jogo)

        # A sessão Steam já foi fechada; abra uma sessão curta só para o ITAD.
        async with aiohttp.ClientSession() as itad_session:
            await asyncio.gather(*(enriquecer_com_limite(itad_session, jogo) for jogo in resultado_final))

    return resultado_final


async def buscar_jogos_por_termo(termo: str, max_resultados: int = 10) -> list[dict]:
    """Faz uma busca normal por título na Steam, sem exigir que esteja em promoção."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Referer": "https://store.steampowered.com/search/",
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        itens = await buscar_pagina_busca(
            session,
            start=0,
            count=RESULTADOS_POR_PAGINA,
            termo=termo,
            apenas_promocoes=False,
        )
        if not itens:
            return []

        termo_normalizado = normalizar_texto(termo)
        itens = sorted(
            itens,
            key=lambda item: (
                normalizar_texto(item.get("name", "")) != termo_normalizado,
                not normalizar_texto(item.get("name", "")).startswith(termo_normalizado),
            ),
        )

        itens_unicos = []
        appids_vistos = set()
        for item in itens:
            appid = extrair_appid(item)
            if appid and appid not in appids_vistos:
                appids_vistos.add(appid)
                itens_unicos.append(item)
            if len(itens_unicos) >= max_resultados:
                break

        semaforo = asyncio.Semaphore(MAX_CONCORRENCIA_STEAM)

        async def montar_com_limite(item: dict) -> dict | None:
            async with semaforo:
                return await montar_jogo(session, item)

        jogos = await asyncio.gather(*(montar_com_limite(item) for item in itens_unicos))
    return [jogo for jogo in jogos if jogo is not None]


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

    if jogo["preco_original"] > 0 and desconto > 0:
        embed.add_field(name="💰 Preço Original", value=f"~~{formatar_real(jogo['preco_original'])}~~", inline=True)
        embed.add_field(name="✅ Com Desconto", value=f"**{formatar_real(jogo['preco_final'])}**", inline=True)
        economia = jogo["preco_original"] - jogo["preco_final"]
        embed.add_field(name="📉 Desconto", value=f"**-{desconto}%**\n(economia {formatar_real(economia)})", inline=True)
    elif jogo["preco_final"] > 0:
        embed.add_field(name="💰 Preço Atual", value=f"**{formatar_real(jogo['preco_final'])}**", inline=False)
    else:
        embed.add_field(name="💰 Preço", value="**GRÁTIS!** 🎉", inline=False)

    # Preço mínimo histórico (via IsThereAnyDeal, se configurado)
    preco_min = jogo.get("preco_minimo_historico")
    if preco_min is not None:
        preco_atual = jogo["preco_final"]
        if preco_atual <= preco_min + 0.01:  # margem pra arredondamento
            comparacao = "🏆 **Esse é o menor preço histórico!**"
        else:
            diferenca = preco_atual - preco_min
            comparacao = f"Já esteve **{formatar_real(diferenca)}** mais barato"

        valor_historico = f"**{formatar_real(preco_min)}**\n{comparacao}"

        data_promo = jogo.get("data_promocao_historica")
        if data_promo:
            try:
                dt = datetime.fromisoformat(data_promo.replace("Z", "+00:00"))
                valor_historico += f"\n📅 Última promoção: {dt.strftime('%d/%m/%Y')}"
            except Exception:
                pass

        embed.add_field(name="📊 Menor Preço Histórico", value=valor_historico, inline=False)

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

    embed.set_footer(text=f"Steam BR • Jogo {indice} de {total} • Expira após 5 min sem interação")
    return embed


# ─── View de paginação (botões) ───────────────────────────────────────────────
class PaginacaoView(View):
    """View com botões para navegar entre os jogos em promoção, um por vez."""

    def __init__(self, jogos: list[dict], autor_id: int, pagina_atual: int = 0):
        super().__init__(timeout=300)  # expira após 5 min de inatividade
        self.jogos = jogos
        self.autor_id = autor_id
        self.pagina_atual = pagina_atual
        self.mensagem: discord.Message | None = None
        self._atualizar_botoes()

    def _atualizar_botoes(self):
        self.anterior.disabled = self.pagina_atual == 0
        self.proximo.disabled = self.pagina_atual >= len(self.jogos) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.autor_id:
            await interaction.response.send_message(
                "❌ Apenas quem pediu a busca pode navegar. Use `!promocoes` ou `!promocao <jogo>` para fazer a sua própria busca!",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self):
        if not self.mensagem:
            return
        try:
            await self.mensagem.delete()
        except discord.HTTPException as erro:
            print(f"[AVISO] Não foi possível apagar uma paginação expirada: {erro}")

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


def criar_embed_resultados_busca(termo: str, jogos: list[dict]) -> discord.Embed:
    """Monta um resumo neutro para a pesquisa por título."""
    embed = discord.Embed(
        title=f"🔎 Resultados para: {termo}",
        description=f"**{len(jogos)} jogo(s)** encontrado(s) na Steam BR.",
        color=discord.Color.from_rgb(23, 153, 232),
        timestamp=datetime.utcnow(),
    )
    for indice, jogo in enumerate(jogos, start=1):
        preco = formatar_real(jogo["preco_final"]) if jogo["preco_final"] > 0 else "GRÁTIS"
        promocao = f" • **-{jogo['desconto']}%**" if jogo["desconto"] else " • sem promoção agora"
        nota = (
            f"{emoji_avaliacao(jogo['avaliacao_percentual'])} {jogo['avaliacao_percentual']}% positivas"
            if jogo["avaliacao_total"]
            else "Sem avaliações"
        )
        embed.add_field(
            name=f"{indice}. {jogo['nome'][:60]}",
            value=f"**{preco}**{promocao}\n{nota}\n[Ver na Steam]({jogo['url']})",
            inline=False,
        )
    embed.set_footer(text="Steam BR • Use os botões ou !ir <número> para abrir um resultado")
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
        view.mensagem = await canal.send(embed=primeiro_embed, view=view)

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

    tempo_estimado = "1 minuto" if ITAD_API_KEY else "30 segundos"
    msg_espera = await ctx.send(
        f"🔍 Buscando promoções na Steam com -{desc}% ou mais, nota ≥{NOTA_MINIMA}%"
        f"{', sem indies' if EXCLUIR_INDIE else ', incluindo indies'}...\n"
        f"⏳ Isso pode levar até {tempo_estimado} (estamos vasculhando várias páginas!)"
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
    view.mensagem = await ctx.send(embed=embed_primeiro, view=view)


@bot.command(name="promocao", aliases=["pesquisar", "busca"])
async def cmd_promocao(ctx, *, termo: str):
    """Pesquisa um jogo pelo nome e mostra seu preço atual e eventual promoção."""
    termo = termo.strip()
    if len(termo) < 2:
        await ctx.send("⚠️ Informe pelo menos duas letras. Exemplo: `!promocao Hades`.")
        return

    msg_espera = await ctx.send(f"🔎 Pesquisando **{termo}** na Steam BR...")
    jogos = await buscar_jogos_por_termo(termo)
    await msg_espera.delete()

    if not jogos:
        await ctx.send(f"😕 Não encontrei jogos para **{termo}**. Tente outro título.")
        return

    ultima_busca[ctx.author.id] = jogos
    await ctx.send(embed=criar_embed_resultados_busca(termo, jogos))
    view = PaginacaoView(jogos, autor_id=ctx.author.id)
    view.mensagem = await ctx.send(embed=criar_embed_jogo(jogos[0], 1, len(jogos)), view=view)


@bot.command(name="ir")
async def cmd_ir(ctx, numero: int):
    """Pula direto para um jogo específico da última busca feita."""
    jogos = ultima_busca.get(ctx.author.id)
    if not jogos:
        await ctx.send("❌ Você ainda não fez nenhuma busca. Use `!promocoes` ou `!promocao <jogo>` primeiro.")
        return
    if numero < 1 or numero > len(jogos):
        await ctx.send(f"❌ Escolha um número entre 1 e {len(jogos)}.")
        return

    view = PaginacaoView(jogos, autor_id=ctx.author.id, pagina_atual=numero - 1)
    embed = criar_embed_jogo(jogos[numero - 1], numero, len(jogos))
    view.mensagem = await ctx.send(embed=embed, view=view)


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


@bot.command(name="noindie")
async def cmd_noindie(ctx, estado: str = None):
    """Ativa o filtro de jogos indie; use 'off' para voltar a incluí-los."""
    global EXCLUIR_INDIE

    if estado is None:
        EXCLUIR_INDIE = True
        await ctx.send("🚫 Filtro de indies ativado. As próximas buscas com `!promocoes` não mostrarão jogos indie.")
        return

    estado = estado.casefold()
    if estado in {"on", "sim", "true", "ativar"}:
        EXCLUIR_INDIE = True
        await ctx.send("🚫 Filtro de indies ativado.")
    elif estado in {"off", "não", "nao", "false", "desativar"}:
        EXCLUIR_INDIE = False
        await ctx.send("✅ Jogos indie voltarão a aparecer nas próximas buscas com `!promocoes`.")
    elif estado in {"status", "estado"}:
        status = "ativado — indies serão excluídos" if EXCLUIR_INDIE else "desativado — indies serão incluídos"
        await ctx.send(f"🚫 Filtro de indies: **{status}**.")
    else:
        await ctx.send("⚠️ Use `!noindie`, `!noindie on`, `!noindie off` ou `!noindie status`.")


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
                            linhas.append(f"   Chaves disponíveis: `{list(items[0].keys())}`")

                            # Testar o processamento completo com diagnóstico detalhado por etapa
                            diagnostico = await processar_jogo_debug(session, items[0])
                            linhas.append(f"   {diagnostico}")

                            # Testar também em mais 2 itens, caso o primeiro seja um caso raro
                            for i, outro_item in enumerate(items[1:3], start=2):
                                diag2 = await processar_jogo_debug(session, outro_item)
                                linhas.append(f"   Item {i} (`{outro_item.get('name', '?')}`): {diag2}")
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



@bot.command(name="ajuda", aliases=["help", "comandos"])
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
        name="🎯 `!promocao <jogo>`",
        value="Pesquisa um jogo pelo nome e mostra o preço atual e a promoção, se houver.",
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
    embed.add_field(
        name="🚫 `!noindie [on|off|status]`",
        value="Exclui jogos indie das próximas buscas. Use sem argumento para ativar.",
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
    # Antes do on_ready a task ainda não foi iniciada; nesse estado
    # next_iteration pode não existir em algumas versões do discord.py.
    proxima = verificar_promocoes.next_iteration if verificar_promocoes.is_running() else None
    proxima_str = proxima.strftime("%d/%m/%Y às %H:%M UTC") if proxima else "Não agendado"

    embed = discord.Embed(title="⚙️ Configurações do Bot", color=discord.Color.blurple())
    embed.add_field(name="📢 Canal", value=canal_nome, inline=True)
    embed.add_field(name="📉 Desconto Mínimo", value=f"-{DESCONTO_MINIMO}%", inline=True)
    embed.add_field(name="⭐ Nota Mínima", value=f"{NOTA_MINIMA}%", inline=True)
    embed.add_field(name="🚫 Excluir Indies", value="Sim" if EXCLUIR_INDIE else "Não", inline=True)
    embed.add_field(name="⏱️ Intervalo", value=f"a cada {INTERVALO_HORAS}h", inline=True)
    embed.add_field(
        name="📊 Preço Mínimo Histórico (ITAD)",
        value="✅ Ativado" if ITAD_API_KEY else "⚠️ Desativado (configure ITAD_API_KEY)",
        inline=True,
    )
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
    elif isinstance(error, commands.CommandInvokeError):
        print(f"[ERRO] Comando {ctx.command}: {error.original}")
        await ctx.send("❌ Ocorreu um erro ao executar esse comando. Tente novamente em alguns segundos.")
    else:
        print(f"[ERRO] Comando {ctx.command}: {error}")


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("❌ ERRO: DISCORD_TOKEN não definido no arquivo .env!")
        exit(1)
    if CANAL_ID == 0:
        print("⚠️  AVISO: CANAL_ID não definido. Use !promocoes manualmente ou configure o .env")
    bot.run(DISCORD_TOKEN)
