# 🎮 Bot de Promoções Steam para Discord

Bot em Python que monitora e envia automaticamente as promoções da Steam no Discord, com **preços em BRL (R$)**, filtro por avaliação, exclusão de jogos indie e navegação por páginas.

---

## ✨ Funcionalidades

- 🔍 **Busca ampla** — vasculha várias páginas de resultados da Steam (não só os "destaques"), retornando dezenas de jogos
- ⭐ **Prioriza jogos bem avaliados** — ordena por % de avaliações positivas, depois por desconto
- 🚫 **Exclui jogos indie** automaticamente (configurável)
- 📄 **Sistema de páginas** — navegue pelos jogos um por um com botões ◀ Anterior / Próximo ▶
- 💰 **Preços em Reais (R$)**
- ⏱️ **Verificação automática periódica** (padrão: a cada 6 horas)
- 🎁 **Jogos grátis da Epic Games** — anuncia automaticamente cada jogo grátis da semana, uma única vez por jogo
- 🇧🇷 **100% em Português Brasileiro**

---

## 🚀 Instalação

Veja o tutorial completo enviado anteriormente. Resumo rápido:

```bash
pip install -r requirements.txt
cp .env.example .env
# edite o .env com seu token e ID do canal
python bot.py
```

---

## 💬 Comandos

| Comando | Descrição |
|---|---|
| `!promocoes` | Busca promoções (bem avaliadas, sem indie), mostra lista + navegação |
| `!promocoes 70` | Só jogos com -70% ou mais |
| `!ir <número>` | Pula direto para o jogo N da sua última busca |
| `!notaminima` | Mostra a nota mínima de avaliação atual |
| `!notaminima 60` | Ajusta a nota mínima para 60% (só nesta sessão) |
| `!config` | Mostra as configurações atuais do bot |
| `!atualizar` | Força verificação imediata *(Admin)* |
| `!epicgratis` | Mostra os jogos grátis atuais na Epic Games Store |
| `!epicatualizar` | Força verificação de jogos grátis da Epic agora *(Admin)* |
| `!ajuda` | Lista todos os comandos |

### Como funciona a navegação

Ao rodar `!promocoes`, o bot manda:
1. Um **resumo** com até 10 jogos em miniatura
2. Um **card detalhado** do 1º jogo, com botões:
   - **◀ Anterior** / **Próximo ▶** — navega um jogo por vez
   - **🔢 Ir para...** — te lembra do comando `!ir <número>` para pular direto

Assim você consegue folhear **todos** os jogos encontrados, não só os primeiros 8-10.

---

## ⚙️ Variáveis de Ambiente

| Variável | Padrão | Descrição |
|---|---|---|
| `DISCORD_TOKEN` | *(obrigatório)* | Token do bot Discord |
| `CANAL_ID` | *(obrigatório)* | ID do canal para promoções automáticas |
| `DESCONTO_MINIMO` | `50` | % mínimo de desconto para notificar |
| `INTERVALO_HORAS` | `6` | Frequência de verificação automática |
| `NOTA_MINIMA` | `70` | % mínimo de avaliações positivas exigido |
| `EXCLUIR_INDIE` | `true` | Se `true`, remove jogos da categoria Indie da lista |
| `INTERVALO_HORAS_EPIC` | `12` | Frequência de verificação dos jogos grátis da Epic Games |

---

## 🎁 Jogos grátis da Epic Games

O bot verifica periodicamente (padrão: a cada 12h) quais jogos estão grátis **agora** na Epic Games Store, usando a mesma API que o launcher oficial da Epic consulta. Cada jogo grátis é anunciado **uma única vez** — o controle de "já anunciado" fica salvo no arquivo `epic_enviados.json`, então mesmo que o bot reinicie (por exemplo, após um redeploy), ele não vai anunciar o mesmo jogo de novo.

O anúncio já vem com o link direto para resgatar o jogo na Epic Games Store.

---

## 🧠 Como o filtro de qualidade funciona

Para cada jogo em promoção, o bot:

1. Confere se o desconto é ≥ `DESCONTO_MINIMO`
2. Busca os **gêneros** do jogo — se tiver "Indie" e `EXCLUIR_INDIE=true`, descarta
3. Busca a **nota de avaliação** direto da Steam (% de reviews positivas)
4. Descarta jogos com menos de 10 avaliações (dado pouco confiável)
5. Descarta jogos abaixo da `NOTA_MINIMA`
6. Ordena o resultado final por **nota de avaliação** (do maior pro menor) e depois por **desconto**

Isso significa: menos jogos pequenos/desconhecidos e obscuros, mais títulos consagrados e bem avaliados no topo da lista.

---

## ⚠️ Sobre a demora da busca

Como o bot agora consulta várias páginas de resultados **e** busca detalhes + avaliação de cada jogo individualmente (para aplicar os filtros), a busca pode levar de 15 a 30 segundos. Isso é esperado — é o preço de ter uma lista mais completa e mais precisa.

---

## 📄 Licença

Uso livre para fins pessoais. A Steam e seus dados são propriedade da Valve Corporation.
