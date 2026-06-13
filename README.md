# WhatsApp Web MCP

Servidor MCP de domínio para agentes consultarem e exportarem conversas
autorizadas do WhatsApp Web. O cliente pede chats, mensagens, mídia,
transcrições ou ações; sessão, busca, seleção, scroll e recuperação ficam
encapsulados no servidor.

> Projeto independente e não oficial. Não é afiliado ao WhatsApp ou à Meta.
> Use somente em contas e conversas que você tem autorização para acessar.

## Recursos

- Perfil persistente `default`, headless por padrão.
- `observe=true` para mostrar somente a operação atual.
- Busca e leitura pelo DOM renderizado, sem SQLite ou IndexedDB local.
- Mensagens recentes sempre reposicionadas no fim da conversa.
- Histórico e pesquisa paginados por cursor opaco.
- Texto, direção, participante, reply, forwarded e tipo de mídia em JSON.
- Captura de mídia pelo `message_id`; associações incertas ficam separadas.
- Transcrição de áudio e vídeo exclusivamente com WhisperX.
- Export persistente com mensagens, replies, mídia e transcrições.
- Autorrecuperação com uma única reinicialização no mesmo perfil.
- Envios protegidos por intenção explícita, preview e confirmação literal.

## API MCP v0.2

O servidor publica somente estas ferramentas:

- `whatsapp_status`
- `whatsapp_list_chats`
- `whatsapp_get_messages`
- `whatsapp_get_media`
- `whatsapp_export_chat`
- `whatsapp_transcribe_file`
- `whatsapp_prepare_action`
- `whatsapp_confirm_action`
- `whatsapp_action_status`

Todas recebem um objeto `request`. Parâmetros de navegador, sessão, seletores,
scroll e Playwright não fazem parte da API.

### Ler mensagens recentes

```json
{
  "request": {
    "selector": "Troco Solidário - Anotações",
    "mode": "recent",
    "limit": 50
  }
}
```

`selector` aceita string ou objeto com `title`, `query`, `phone` e `jid`.
Depois de `whatsapp_list_chats`, prefira o `chat_id` retornado.

### Histórico e pesquisa

```json
{
  "request": {
    "chat_id": "chat_...",
    "mode": "search",
    "query": "contrato",
    "message_types": ["text", "document", "audio"],
    "date_from": "2026-06-01",
    "date_to": "2026-06-13",
    "limit": 100,
    "cursor": "cur_..."
  }
}
```

Modos disponíveis: `recent`, `history` e `search`. A resposta inclui
`latest_boundary_verified`, métricas de paginação e `next_cursor`.

### Capturar mídia

```json
{
  "request": {
    "chat_id": "chat_...",
    "message_id": "3EB0...",
    "transcribe": true,
    "diarize": false
  }
}
```

Áudio e vídeo podem ser transcritos com WhisperX. Imagens, stickers, GIFs e
documentos retornam o caminho do arquivo capturado.

### Mostrar uma operação

```json
{
  "request": {
    "chat_id": "chat_...",
    "mode": "recent",
    "limit": 10,
    "observe": true
  }
}
```

O override vale somente para essa chamada. A sessão volta ao modo headless ao
terminar.

## Estado de suporte

| Recurso | Estado |
| --- | --- |
| Listar e localizar chats | Suportado |
| Ler recente, histórico e pesquisa | Suportado |
| Exportar JSON | Suportado |
| Capturar imagem, sticker e mídia renderizada | Suportado |
| Capturar/transcrever áudio e vídeo | Suportado quando a mídia e o WhisperX estão disponíveis |
| Enviar texto | Verificado, sempre com confirmação |
| Enviar documento, incluindo `.zip` | Verificado, sempre com confirmação |
| Enviar outras mídias, reply ou encaminhamento | Bloqueado até smoke confirmado específico |
| SQLite/IndexedDB local | Não utilizado |

## Instalação

Requisitos: Python 3.11 ou 3.12, Chrome/Chromium e FFmpeg.

```bash
git clone https://github.com/KingDonRush/whatsapp-web-mcp.git
cd whatsapp-web-mcp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
python -m playwright install chromium
```

O servidor usa transporte MCP `stdio`:

```bash
whatsapp-web-mcp
```

## Configuração do harness

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

Comece com `whatsapp_status`. Se a conta não estiver autenticada, a resposta
será `login_required` com um `qr_artifact`. Depois do login, o perfil
persistente `default` será reutilizado.

## Confirmação de envio

O envio é sempre uma operação em duas etapas:

1. `whatsapp_prepare_action` exige ordem explícita do usuário e retorna preview,
   `action_id` e a frase exata de confirmação.
2. `whatsapp_confirm_action` só despacha com
   `CONFIRMO ENVIAR <action_id>` e `user_confirmed=true`.

Exemplo:

```json
{
  "request": {
    "type": "send_document",
    "chat_id": "chat_...",
    "file_path": "/absolute/arquivo.zip",
    "caption": "Entrega",
    "user_order_text": "Envie este arquivo para o grupo"
  }
}
```

Leituras e exports nunca enviam mensagens. A confirmação é consumida
atomicamente após sucesso para impedir replay.

## WhisperX

```bash
python -m pip install -e '.[transcription]'
```

Ou configure uma venv separada:

```bash
python3.11 -m venv .venv-whisperx
.venv-whisperx/bin/pip install whisperx
export WHISPERX_PYTHON="$PWD/.venv-whisperx/bin/python"
```

Formatos aceitos incluem os formatos de áudio definidos pelo FFmpeg/WhisperX
como WAV, MP3, M4A, OGG, OPUS, FLAC e WebM, além de vídeos como MP4, MOV, MKV,
AVI e WebM. Vídeos têm o áudio extraído antes da transcrição. Diarização usa
`diarize=true` e pode receber `min_speakers` e `max_speakers`.

## Docker e VPS

```bash
docker build -t whatsapp-web-mcp .
mkdir -p runtime
sudo chown -R 10001:10001 runtime
docker run --rm -i -v "$PWD/runtime:/data" whatsapp-web-mcp
```

Em VPS, execute o servidor atrás de um harness autenticado ou por SSH `stdio`.
Não exponha o processo diretamente à internet. O volume persistente deve
proteger o perfil autenticado, exports e ações pendentes.

## Dados sensíveis

```text
WHATSAPP_MCP_DATA_DIR/
├── state/
│   ├── browser-profiles/default/
│   └── domain/
└── exports/
```

Nunca publique `state/`, `exports/`, QR codes, transcrições, tokens ou arquivos
de mídia. O `.gitignore` também exclui `node_modules/`, ambientes locais e
arquivos `.env`.

Variáveis principais:

| Variável | Uso |
| --- | --- |
| `WHATSAPP_MCP_DATA_DIR` | Raiz de estado e exports |
| `WHATSAPP_MCP_STATE_DIR` | Override do estado |
| `WHATSAPP_MCP_OUTPUT_DIR` | Override dos exports |
| `WHATSAPP_MCP_BROWSER_BIN` | Chrome/Chromium específico |
| `FFMPEG_BIN` | Executável FFmpeg |
| `WHISPERX_PYTHON` | Python com WhisperX |
| `WHISPERX_MODEL_DIR` | Cache dos modelos |
| `HF_TOKEN` | Token opcional para diarização |

## Desenvolvimento

```bash
python -m unittest discover -s tests -v
python -m compileall server.py whatsapp_web_mcp tests
```

Licença MIT. Consulte [SECURITY.md](SECURITY.md) antes de hospedar uma instância
autenticada.
