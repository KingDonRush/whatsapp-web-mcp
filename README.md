# WhatsApp Web MCP

Servidor MCP local para agentes pesquisarem, estruturarem e exportarem conversas
autorizadas do WhatsApp Web. Usa Playwright e o DOM renderizado, preserva
respostas citadas e metadados de mídia, e pode transcrever áudio/vídeo com
WhisperX.

> Projeto independente e não oficial. Não é afiliado ao WhatsApp ou à Meta.
> Use somente em contas, conversas e ambientes que você tem autorização para
> acessar. Automação pode estar sujeita aos termos do WhatsApp.

## Principais recursos

- Sessão persistente do WhatsApp Web.
- Headless por padrão, com override headed por operação.
- QR de login salvo como artefato quando autenticação for necessária.
- Busca de contatos e mensagens pelo DOM/acessibilidade.
- Exportação JSON com direção, horário, remetente, reply, forwarded e mídia.
- Rolagem histórica por intervalo de datas com métricas de cobertura.
- Captura de mídia vinculada ao balão; capturas incertas ficam separadas.
- Transcrição WhisperX de áudio e vídeo, com diarização opcional.
- GPU automática quando a ocupação de memória estiver em até 65%.
- Envio protegido por intenção explícita, preview, token e confirmação literal.

## Estado de suporte

| Recurso | Estado |
| --- | --- |
| Buscar contatos e mensagens | Suportado |
| Exportar conversa e metadados | Suportado |
| Exportar/transcrever áudio e vídeo | Suportado quando mídia e WhisperX estão disponíveis |
| Enviar texto | Verificado, sempre com confirmação |
| Enviar documentos, incluindo `.zip` | Verificado, sempre com confirmação |
| Preview de imagem/documento/áudio | Verificado sem envio |
| Reply nativo | Preview verificado sem envio |
| Enviar outras mídias, reply ou encaminhamento | Bloqueado até smoke específico com confirmação |
| SQLite/IndexedDB local | Não utilizado |

## Requisitos

- Python 3.11 ou 3.12.
- Chrome, Chromium ou navegador instalado pelo Playwright.
- FFmpeg para preparar áudio e extrair áudio de vídeos.
- WhisperX opcional para transcrição.

## Instalação local

```bash
git clone https://github.com/KingDonRush/whatsapp-web-mcp.git
cd whatsapp-web-mcp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
python -m playwright install chromium
```

No Debian/Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg chromium
```

Teste o servidor:

```bash
whatsapp-web-mcp
```

O processo usa transporte MCP `stdio`, portanto normalmente deve ser iniciado
por Codex, Claude Desktop ou outro harness MCP.

## Configuração do harness

Exemplo genérico:

```json
{
  "mcpServers": {
    "whatsapp-web": {
      "command": "/absolute/path/whatsapp-web-mcp/.venv/bin/whatsapp-web-mcp",
      "args": [],
      "env": {
        "WHATSAPP_MCP_DATA_DIR": "/absolute/persistent/path/whatsapp-web-mcp"
      }
    }
  }
}
```

Exemplo para Codex:

```toml
[mcp_servers.whatsapp-web]
command = "/absolute/path/whatsapp-web-mcp/.venv/bin/whatsapp-web-mcp"
enabled = true
startup_timeout_sec = 30.0

[mcp_servers.whatsapp-web.env]
WHATSAPP_MCP_DATA_DIR = "/absolute/persistent/path/whatsapp-web-mcp"
```

Na primeira operação, chame `whatsapp_browser_open`. Em headless, se a conta
não estiver autenticada, o resultado inclui o caminho de um `qr_artifact`.
Depois do login, o perfil persistente é reutilizado.

## Docker

```bash
docker build -t whatsapp-web-mcp .
mkdir -p runtime
sudo chown -R 10001:10001 runtime
docker run --rm -i \
  -v "$PWD/runtime:/data" \
  whatsapp-web-mcp
```

Para integrar com um harness, use `docker` como comando MCP e passe
`run --rm -i -v /absolute/runtime:/data whatsapp-web-mcp` como argumentos.
O volume é obrigatório para preservar login, política do navegador e exports.

O Dockerfile base não instala WhisperX, porque CUDA/Torch variam por host. Para
transcrição, use uma imagem derivada compatível com sua GPU ou a instalação
local descrita abaixo.

## VPS

A opção mais simples é instalar o projeto e o harness MCP na própria VPS. Outra
opção segura é usar SSH como transporte `stdio`:

```json
{
  "mcpServers": {
    "whatsapp-web-vps": {
      "command": "ssh",
      "args": [
        "user@example-vps",
        "/opt/whatsapp-web-mcp/.venv/bin/whatsapp-web-mcp"
      ]
    }
  }
}
```

Mantenha `WHATSAPP_MCP_DATA_DIR` em volume persistente na VPS. Não exponha o
processo `stdio` diretamente à internet; autenticação e autorização devem ficar
no harness ou no túnel SSH.

## WhisperX

Instalação no mesmo ambiente:

```bash
python -m pip install -e '.[transcription]'
```

Ou use uma venv separada:

```bash
python3.11 -m venv .venv-whisperx
.venv-whisperx/bin/pip install whisperx
export WHISPERX_PYTHON="$PWD/.venv-whisperx/bin/python"
```

Variáveis:

```bash
export FFMPEG_BIN=/usr/bin/ffmpeg
export WHISPERX_PYTHON=/path/to/whisperx-venv/bin/python
export WHISPERX_MODEL_DIR="$HOME/.cache/whisperx"
```

Para diarização, configure `HF_TOKEN` somente no ambiente do processo. Nunca
grave tokens no repositório ou no arquivo de configuração do harness.

## Dados e privacidade

O diretório de dados contém material sensível:

```text
WHATSAPP_MCP_DATA_DIR/
├── state/
│   ├── browser-profiles/
│   ├── browser-artifacts/
│   ├── browser-policy.json
│   └── pending-sends/
└── exports/
```

O perfil do navegador equivale a uma sessão autenticada. Proteja o diretório
com permissões do usuário de serviço, armazenamento criptografado e backups
controlados. Nunca publique `state/`, exports, QR codes ou tokens pendentes.

Variáveis suportadas:

| Variável | Uso |
| --- | --- |
| `WHATSAPP_MCP_DATA_DIR` | Raiz de dados, estado e exports |
| `WHATSAPP_MCP_STATE_DIR` | Override apenas do estado |
| `WHATSAPP_MCP_OUTPUT_DIR` | Override apenas dos exports |
| `WHATSAPP_MCP_BROWSER_BIN` | Chrome/Chromium específico |
| `FFMPEG_BIN` | Executável FFmpeg |
| `WHISPERX_PYTHON` | Python que possui WhisperX |
| `WHISPERX_MODEL_DIR` | Cache/modelos WhisperX |
| `HF_TOKEN` | Token opcional para diarização |

## Confirmação de envio

O fluxo de envio possui duas etapas:

1. `whatsapp_prepare_send_message` exige ordem explícita do usuário e cria um
   token com preview do conteúdo.
2. `whatsapp_confirm_send_message` somente despacha quando recebe literalmente
   `CONFIRMO ENVIAR <token>` e `user_already_confirmed=true`.

Buscas, exports, probes e previews nunca substituem essa confirmação.

## Ferramentas MCP

- `whatsapp_browser_open`, `whatsapp_browser_close`
- `whatsapp_browser_policy`, `whatsapp_set_browser_policy`
- `whatsapp_find_contacts`, `whatsapp_select_context`
- `whatsapp_search_messages`, `whatsapp_chat_structure`
- `whatsapp_export_conversation`
- `whatsapp_transcribe_file`
- `whatsapp_prepare_send_message`, `whatsapp_confirm_send_message`
- `whatsapp_probe_send_media`
- `whatsapp_probe_reply_to_message`
- `whatsapp_capabilities`, `whatsapp_sources`

## Desenvolvimento

```bash
python -m unittest discover -s tests -v
python -m compileall server.py whatsapp_web_mcp tests
```

Licença MIT. Consulte [SECURITY.md](SECURITY.md) antes de hospedar ou compartilhar
uma instância autenticada.
