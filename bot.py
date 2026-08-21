import discord
from discord.ext import commands, tasks
from discord.ui import View, Button
import aiohttp
import asyncio
import os
import json
from datetime import datetime
import datetime as dt
from dotenv import load_dotenv

load_dotenv()

# ─── Configurações ───────────────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CANAL_ID = int(os.getenv("CANAL_ID", "0"))
CANAL_ID_EPIC = int(os.getenv("CANAL_ID_EPIC", "0"))    # canal #promos-epic — se não configurado, cai no CANAL_ID
DESCONTO_MINIMO = int(os.getenv("DESCONTO_MINIMO", "50"))
INTERVALO_HORAS = int(os.getenv("INTERVALO_HORAS", "6"))
NOTA_MINIMA = int(os.getenv("NOTA_MINIMA", "70"))       # % mínimo de avaliações positivas
EXCLUIR_INDIE = os.getenv("EXCLUIR_INDIE", "true").lower() == "true"
ITAD_API_KEY = os.getenv("ITAD_API_KEY", "")            # chave gratuita: isthereanydeal.com/apps/my/
JOGOS_POR_PAGINA = 10

jogos_enviados = set()

# Arquivo onde ficam guardados os IDs dos jogos grátis da Epic já anunciados —
# persistido em disco pra sobreviver a reinícios/redeploys do bot (memória sozinha se perderia).
EPIC_ENVIADOS_ARQUIVO = "epic_enviados.json"

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

# API não-oficial, mas é a mesma que o launcher da Epic usa internamente — estável e sem necessidade de chave.
EPIC_FREEGAMES_URL = "https://store-site-backend-static-ipv4.ak.epicgames.com/freeGamesPromotions"

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


async def processar_jogo(session: aiohttp.ClientSession, item: dict, desconto_minimo: int,
                          nota_minima: int, excluir_indie: bool) -> dict | None:
    """Processa um item bruto da busca: pega detalhes + avaliação, aplica filtros."""
    appid = item.get("id") or item.get("appid")
    if not appid and item.get("logo"):
        import re
        m = re.search(r"/apps/(\d+)/", item.get("logo", ""))
        if m:
            appid = int(m.group(1))
    if not appid:
        return None

    detalhes = await buscar_detalhes_jogo(session, appid)
    if not detalhes:
        return None

    tipo_jogo = detalhes.get("type")
    if tipo_jogo is not None and tipo_jogo != "game":
        return None

    preco_info = detalhes.get("price_overview", {})
    discount = preco_info.get("discount_percent", 0)
    if discount < desconto_minimo:
        return None

    generos = [g.get("description", "") for g in detalhes.get("genres", [])]
    if excluir_indie and GENRE_INDIE in generos:
        return None

    avaliacao = await buscar_avaliacao(session, appid)
    if not avaliacao or avaliacao["total"] < 10:
        return None
    if avaliacao["percentual"] < nota_minima:
        return None

    preco_original = preco_info.get("initial", 0) / 100
    preco_final = preco_info.get("final", 0) / 100

    preco_minimo_historico = None
    data_promocao_historica = None
    itad_id = await buscar_itad_game_id(session, appid)
    if itad_id:
        historico = await buscar_preco_minimo_historico(session, itad_id)
        if historico:
            preco_minimo_historico = historico.get("preco_minimo")
            data_promocao_historica = historico.get("data_promocao")

    return {
        "appid": appid,
        "nome": detalhes.get("name", item.get("name", "Desconhecido")),
        "desconto": discount,
        "preco_original": preco_original,
        "preco_final": preco_final,
        "preco_minimo_historico": preco_minimo_historico,
        "data_promocao_historica": data_promocao_historica,
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
        for pagina in range(max_paginas):
            start = pagina * 50
            items = await buscar_pagina_busca(session, start=start, count=50)
            if not items:
                break
            resultados_brutos.extend(items)
            if len(resultados_brutos) >= 300:
                break
            await asyncio.sleep(0.3)

        if not resultados_brutos:
            return []

        semaforo = asyncio.Semaphore(15)

        async def processar_com_limite(item):
            async with semaforo:
                return await processar_jogo(session, item, desconto_minimo, nota_minima, excluir_indie)

        tarefas = [processar_com_limite(item) for item in resultados_brutos]
        processados = await asyncio.gather(*tarefas)

    jogos_validos = [j for j in processados if j is not None]

    vistos = set()
    resultado_final = []
    for jogo in jogos_validos:
        if jogo["appid"] not in vistos:
            vistos.add(jogo["appid"])
            resultado_final.append(jogo)

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

    preco_min = jogo.get("preco_minimo_historico")
    if preco_min is not None:
        preco_atual = jogo["preco_final"]
        if preco_atual <= preco_min + 0.01:
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

    embed.set_footer(text=f"Steam BR • Jogo {indice} de {total} • Página")
    return embed


class PaginacaoView(View):
    """View com botões para navegar entre os jogos em promoção, um por vez."""

    def __init__(self, jogos: list[dict], autor_id: int, pagina_atual: int = 0):
        super().__init__(timeout=180)
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
def carregar_epic_enviados() -> set:
    """Carrega do disco os IDs dos jogos da Epic já anunciados, sobrevivendo a reinícios do bot."""
    try:
        with open(EPIC_ENVIADOS_ARQUIVO, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def salvar_epic_enviados(ids: set) -> None:
    """Persiste no disco os IDs já anunciados."""
    try:
        with open(EPIC_ENVIADOS_ARQUIVO, "w", encoding="utf-8") as f:
            json.dump(list(ids), f)
    except Exception as e:
        print(f"[ERRO] Falha ao salvar {EPIC_ENVIADOS_ARQUIVO}: {e}")


epic_jogos_enviados = carregar_epic_enviados()


async def buscar_jogos_gratis_epic() -> list[dict]:
    """
    Busca os jogos GRÁTIS ATUAIS (não os futuros) na Epic Games Store.
    """
    params = {"locale": "pt-BR", "country": "BR", "allowCountries": "BR"}
    jogos_gratis = []

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(EPIC_FREEGAMES_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    print(f"[AVISO] Epic Games API retornou status {resp.status}")
                    return []
                data = await resp.json(content_type=None)
    except Exception as e:
        print(f"[ERRO] Falha ao buscar jogos grátis da Epic: {e}")
        return []

    elementos = data.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", [])

    for jogo in elementos:
        promocoes = jogo.get("promotions")
        if not promocoes:
            continue

        ofertas_ativas = promocoes.get("promotionalOffers", [])
        esta_gratis_agora = False
        for grupo in ofertas_ativas:
            for oferta in grupo.get("promotionalOffers", []):
                desconto_pct = oferta.get("discountSetting", {}).get("discountPercentage")
                if desconto_pct == 0:
                    esta_gratis_agora = True
                    break

        if not esta_gratis_agora:
            continue

        preco_info = jogo.get("price", {}).get("totalPrice", {})
        preco_original_centavos = preco_info.get("originalPrice", 0)

        # offerMappings[0].pageSlug é o campo confiável pra montar a URL —
        # productSlug frequentemente vem null ou com "/home" grudado (link quebrado).
        # Ordem de fallback, do mais confiável pro menos:
        page_slug = None
        offer_mappings = jogo.get("offerMappings") or []
        if offer_mappings and offer_mappings[0].get("pageSlug"):
            page_slug = offer_mappings[0]["pageSlug"]
        elif jogo.get("catalogNs", {}).get("mappings"):
            catalog_mappings = jogo["catalogNs"]["mappings"]
            if catalog_mappings and catalog_mappings[0].get("pageSlug"):
                page_slug = catalog_mappings[0]["pageSlug"]
        elif jogo.get("productSlug"):
            page_slug = jogo["productSlug"].replace("/home", "")

        url = f"https://store.epicgames.com/pt-BR/p/{page_slug}" if page_slug else "https://store.epicgames.com/pt-BR/free-games"

        imagem = ""
        for img in jogo.get("keyImages", []):
            if img.get("type") in ("OfferImageWide", "featuredMedia", "Thumbnail"):
                imagem = img.get("url", "")
                if img.get("type") == "OfferImageWide":
                    break

        jogos_gratis.append({
            "id": jogo.get("id"),
            "nome": jogo.get("title", "Jogo desconhecido"),
            "descricao": jogo.get("description", ""),
            "preco_original": preco_original_centavos / 100,
            "url": url,
            "imagem": imagem,
        })

    return jogos_gratis


def criar_embed_epic(jogo: dict) -> discord.Embed:
    """Cria o embed de anúncio de um jogo grátis da Epic Games."""
    embed = discord.Embed(
        title=f"🎁 {jogo['nome']}",
        url=jogo["url"],
        description=jogo.get("descricao", "")[:200] or None,
        color=discord.Color.from_rgb(255, 255, 255),
        timestamp=datetime.utcnow(),
    )
    if jogo["preco_original"] > 0:
        embed.add_field(
            name="💰 De",
            value=f"~~{formatar_real(jogo['preco_original'])}~~ → **GRÁTIS**",
            inline=False,
        )
    else:
        embed.add_field(name="💰 Preço", value="**GRÁTIS**", inline=False)

    if jogo.get("imagem"):
        embed.set_image(url=jogo["imagem"])
    embed.set_footer(text="Epic Games Store • Resgate antes que a promoção acabe!")
    return embed


# Quinta (a Epic sempre troca os jogos grátis às 13h BRT de quinta) e segunda
# (checagem extra, caso a de quinta falhe por algum motivo).
# discord.py trabalha em UTC — 18h em Brasília (UTC-3) equivale a 21h UTC.
HORARIOS_VERIFICACAO_EPIC = [
    dt.time(hour=21, minute=0, tzinfo=dt.timezone.utc),  # 18h BRT
]


async def checar_e_anunciar_epic():
    """
    Lógica de fato: busca jogos grátis da Epic e anuncia os que ainda não
    foram enviados. Reaproveitada tanto pela task automática (quinta/segunda)
    quanto pelo comando manual !epicatualizar (que força em qualquer dia).
    """
    await bot.wait_until_ready()
    canal_id_alvo = CANAL_ID_EPIC or CANAL_ID
    canal = bot.get_channel(canal_id_alvo)
    if not canal:
        print(f"[AVISO] Canal {canal_id_alvo} não encontrado (Epic Games).")
        return

    print("[INFO] Verificando jogos grátis da Epic Games...")
    jogos = await buscar_jogos_gratis_epic()

    if not jogos:
        print("[INFO] Nenhum jogo grátis encontrado na Epic Games no momento.")
        return

    novos = [j for j in jogos if j["id"] not in epic_jogos_enviados]
    if not novos:
        print("[INFO] Nenhum jogo novo da Epic Games para anunciar (já enviados antes).")
        return

    for jogo in novos:
        embed = criar_embed_epic(jogo)
        await canal.send(
            content=f"🎁 **Jogo grátis na Epic Games!** Resgate: {jogo['url']}",
            embed=embed,
        )
        epic_jogos_enviados.add(jogo["id"])
        await asyncio.sleep(1)

    salvar_epic_enviados(epic_jogos_enviados)
    print(f"[INFO] {len(novos)} jogo(s) grátis da Epic anunciado(s) para #{canal.name}")


@tasks.loop(time=HORARIOS_VERIFICACAO_EPIC)
async def verificar_jogos_gratis_epic():
    """
    Roda todo dia às 18h BRT, mas só age de fato às quintas e segundas —
    dias em que a Epic Games costuma trocar/confirmar os jogos grátis da semana.
    """
    hoje = dt.datetime.now(dt.timezone.utc).weekday()  # 0=segunda, 3=quinta
    if hoje not in (0, 3):
        return
    await checar_e_anunciar_epic()


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
        f"🔍 Buscando promoções na Steam com -{desc}% ou mais, nota ≥{NOTA_MINIMA}%, sem indies...\n"
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

                            diagnostico = await processar_jogo_debug(session, items[0])
                            linhas.append(f"   {diagnostico}")

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
    embed.add_field(
        name="🎁 `!epicgratis`",
        value="Mostra os jogos grátis atuais na Epic Games Store.",
        inline=False,
    )
    embed.add_field(
        name="🔄 `!epicatualizar`",
        value="Força verificação de jogos grátis da Epic agora. (Admin)",
        inline=False,
    )
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
    embed.add_field(
        name="📊 Preço Mínimo Histórico (ITAD)",
        value="✅ Ativado" if ITAD_API_KEY else "⚠️ Desativado (configure ITAD_API_KEY)",
        inline=True,
    )
    embed.add_field(name="🔄 Próxima Verificação", value=proxima_str, inline=False)

    canal_epic = bot.get_channel(CANAL_ID_EPIC or CANAL_ID)
    canal_epic_nome = f"#{canal_epic.name}" if canal_epic else "❌ Não configurado"
    proxima_epic = verificar_jogos_gratis_epic.next_iteration
    proxima_epic_str = proxima_epic.strftime("%d/%m/%Y às %H:%M UTC") if proxima_epic else "Não agendado"
    embed.add_field(name="🎁 Epic Games — Canal", value=canal_epic_nome, inline=True)
    embed.add_field(name="🎁 Epic Games — Dias", value="Quintas e segundas, 18h", inline=True)
    embed.add_field(name="🎁 Epic Games — Próxima Checagem", value=proxima_epic_str, inline=True)
    embed.add_field(name="🎁 Epic Games — Já Anunciados", value=str(len(epic_jogos_enviados)), inline=True)

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


@bot.command(name="epicgratis", aliases=["epic", "gratisepic"])
async def cmd_epic_gratis(ctx):
    """Mostra os jogos grátis atuais da Epic Games, mesmo que já tenham sido anunciados antes."""
    msg = await ctx.send("🔍 Verificando jogos grátis na Epic Games...")
    jogos = await buscar_jogos_gratis_epic()
    await msg.delete()

    if not jogos:
        await ctx.send("😔 Nenhum jogo grátis encontrado na Epic Games no momento.")
        return

    await ctx.send(f"🎁 **{len(jogos)} jogo(s) grátis atualmente na Epic Games:**")
    for jogo in jogos:
        embed = criar_embed_epic(jogo)
        await ctx.send(embed=embed)
        await asyncio.sleep(0.5)


@bot.command(name="epicatualizar")
@commands.has_permissions(administrator=True)
async def cmd_epic_atualizar(ctx):
    """Força a verificação de jogos grátis da Epic agora, mesmo fora dos dias programados. (Admin)"""
    await ctx.send("🔄 Forçando verificação de jogos grátis da Epic Games agora...")
    await checar_e_anunciar_epic()
    await ctx.send("✅ Verificação concluída!")


@cmd_epic_atualizar.error
async def epic_atualizar_error(ctx, error):
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
    if not verificar_jogos_gratis_epic.is_running():
        verificar_jogos_gratis_epic.start()
        print("   Task de jogos grátis da Epic Games iniciada!")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.send("❓ Comando desconhecido. Use `!ajuda` para ver os comandos disponíveis.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("⚠️ Argumento faltando. Use `!ajuda` para ver como usar o comando.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("⚠️ Argumento inválido — verifique se digitou um número corretamente.")


# ─── Mini servidor web — só pra hospedagens que exigem uma porta HTTP aberta ──
# O Render (no plano gratuito) só oferece "Web Service", que precisa responder
# em alguma porta HTTP pra não ser derrubado. O bot em si não precisa de servidor
# web nenhum — isso aqui existe só pra satisfazer esse requisito da hospedagem,
# e também dá um endpoint pro UptimeRobot "bater" e manter o serviço acordado.
from aiohttp import web

PORTA_WEB = int(os.getenv("PORT", "8080"))  # Render injeta a variável PORT automaticamente


async def handler_status(request):
    status = "conectado" if bot.is_ready() else "iniciando"
    return web.json_response({
        "status": status,
        "bot": str(bot.user) if bot.user else None,
    })


async def iniciar_servidor_web():
    app = web.Application()
    app.router.add_get("/", handler_status)
    app.router.add_get("/status", handler_status)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORTA_WEB)
    await site.start()
    print(f"✅ Mini servidor web rodando na porta {PORTA_WEB} (só pra manter o serviço vivo no Render)")


async def main():
    if not DISCORD_TOKEN:
        print("❌ ERRO: DISCORD_TOKEN não definido no arquivo .env!")
        exit(1)
    if CANAL_ID == 0:
        print("⚠️  AVISO: CANAL_ID não definido. Use !promocoes manualmente ou configure o .env")

    # Sobe o mini servidor web e o bot do Discord ao mesmo tempo, no mesmo processo
    await iniciar_servidor_web()
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
