# Growth Bot

Bot privado no Telegram para gerenciar contas e campanhas do Instagram e usar o editor de vídeo hospedado no Railway.

## Preparação

1. Use Python 3.12.
2. Instale as dependências com `python -m pip install -r requirements.txt`.
3. Copie `.env.example` para `.env` apenas no computador local e preencha os valores.
4. Em produção, cadastre as mesmas variáveis no painel do serviço. Nunca envie `.env` ao GitHub.
5. Execute `database/migration_2026_08_14.sql` uma vez no SQL Editor do Supabase.
6. Inicie com `python main.py`.

## Variáveis obrigatórias do bot

- `TELEGRAM_TOKEN` e `TELEGRAM_OWNER_ID`
- `SUPABASE_URL` e `SUPABASE_KEY`
- `SESSION_ENCRYPTION_KEY` (chave Fernet; não troque enquanto houver dados criptografados)
- `VIDEO_API_URL` e `VIDEO_API_SECRET`, caso o editor seja usado

O login do Instagram usa conexão direta por padrão. Uma variável antiga `INSTAGRAM_PROXY` não será usada enquanto `INSTAGRAM_USE_PROXY` não estiver explicitamente definida como `true`.

## Conectar uma conta

No chat privado com o bot, use `/conta_add @usuario senha`. A mensagem com a senha é removida do chat. Se o Instagram exigir verificação, o bot solicita o código de 6 dígitos ou o código de backup de 8 dígitos sem bloquear os demais comandos.

As sessões e a identidade do dispositivo são mantidas estáveis. O backup da sessão é criptografado no Supabase.

## Editor de vídeo

- `/fundo`: cadastra o fundo 1080x1920.
- `/video`: processa um vídeo com a configuração atual.
- `/video_lote`: processa até 10 vídeos.
- `/video_editor`: abre o editor visual para arrastar e redimensionar com mouse ou toque.
- `/config_video`: mostra ou altera largura, posição, qualidade, FPS, espelhamento e remoção automática de bordas.
- `/video_status`: verifica a API e o FFmpeg.

O editor remove automaticamente bordas pretas ou brancas estáveis antes de posicionar o conteúdo sobre o fundo.

## Verificação

```text
python -m compileall -q .
python -m unittest discover -s tests -v
```

Os segredos anteriormente publicados ou exibidos em capturas de tela devem ser revogados e substituídos nos provedores.
