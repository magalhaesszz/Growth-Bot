# Growth Bot

Bot privado no Telegram para gerenciar contas/campanhas do Instagram e usar o editor de vídeo hospedado em uma API separada.

## Preparação

1. Use Python 3.12.
2. Instale as dependências com `python -m pip install -r requirements.txt`.
3. Copie `.env.example` para `.env` apenas no computador local e preencha os valores.
4. Em produção, cadastre as mesmas variáveis no painel do serviço. Nunca envie `.env` ao GitHub.
5. **Banco novo:** execute `database/schema.sql` uma vez no SQL Editor do Supabase.
6. **Banco já existente:** execute `database/migration_2026_08_19_stability.sql`; ela é idempotente e pode ser reaplicada.
7. No Supabase Storage, garanta que exista o bucket `videos` com a política de acesso adequada ao seu projeto.
8. Inicie com `python main.py`.

## Variáveis

Obrigatórias:

- `TELEGRAM_TOKEN` e `TELEGRAM_OWNER_ID`
- `SUPABASE_URL` e `SUPABASE_KEY`
- `SESSION_ENCRYPTION_KEY` (Fernet; não troque enquanto houver credenciais/sessões criptografadas)
- `VIDEO_API_URL` e `VIDEO_API_SECRET`, caso o editor seja usado

Opcionais relevantes:

- `INSTAGRAM_USE_PROXY=false` por padrão. `INSTAGRAM_PROXY` só é usada quando essa flag está explicitamente em `true`.
- `VIDEO_MAX_FILE_MB=45` limita um único vídeo antes de carregá-lo/processá-lo em memória.
- `VIDEO_MAX_BATCH_MB=120` limita a soma de um lote.
- `VIDEO_SETTINGS_REMOTE=true` persiste as configurações do editor na tabela `video_settings`; o arquivo em `/tmp` fica apenas como cache/fallback.

## Sessões e risco

As sessões e a identidade simulada do dispositivo são mantidas estáveis. O backup da sessão fica criptografado no Supabase e os arquivos locais são gravados de forma atômica.

Uma sessão expirada pausa a conta e gera **uma única notificação** até que a sessão seja renovada. Quando um novo `SESSIONID` é validado e salvo, somente a pausa cujo motivo é `Sessão expirada` é removida; pausas por challenge, spam, taxa de erro ou outro risco continuam bloqueando a conta até revisão.

O estado de risco é persistido em `bot_state`, portanto um restart não deve liberar silenciosamente uma conta pausada. Os alertas disparados dentro de workers/threads são encaminhados ao event loop principal do Telegram.

## Limites e concorrência

Os limites diários usam o dia civil de `America/Sao_Paulo`, o mesmo fuso do scheduler. O modo manual pode ignorar a janela de horário, mas **não ignora limite diário nem aquecimento**.

As ações de uma mesma conta são serializadas no processo para que follow, unfollow, modo manual e limpeza externa não disputem a mesma sessão/limite ao mesmo tempo. Parar o modo manual usa sinalização cooperativa: o bot termina a chamada de rede que já estiver em andamento e não inicia a próxima ação.

A tabela `ig_action_queue` continua disponível para inspeção/uso administrativo, mas o bot não faz retry cego de follow/unfollow. Quando uma chamada externa termina em estado incerto, repetir automaticamente pode duplicar um efeito no Instagram.

## Unfollow seguro

A limpeza de não-seguidores externos não usa mais o banco como prova de que alguém não segue de volta. A relação é consultada ao vivo e verificada novamente antes do unfollow. Whitelist e limite diário são respeitados, e falha/timeout na consulta da relação resulta em **não remover** aquele perfil.

O auto-unfollow de follow-backs também falha fechado: só remove quando `followed_by=True` foi confirmado ao vivo. Erro de rede nunca é interpretado como autorização para remover.

## Telegram e permissões

O proprietário sempre é administrador. Usuários comuns autorizados podem consultar informações e usar o editor de vídeo; ações que alteram operação do Instagram (limites, alvos, campanhas, pausar/retomar, modo manual, limpeza de não-seguidores etc.) exigem `is_admin=true`.

O painel de configuração e a limpeza externa usam a **conta selecionada** no painel, evitando aplicar uma ação à primeira conta da lista por engano.

## Editor de vídeo

Comandos principais:

- `/download`: baixa um link ou recebe um `.mp4` para edição.
- `/fundo` e `/fundos`: gerenciam fundos.
- `/video_lote`: processa até 10 vídeos, respeitando também o limite total de MB.
- `/config_video`: mostra/altera largura, posição, qualidade, FPS, espelhamento e crop automático.
- `/video_status`: verifica a API e o FFmpeg.
- `/biblioteca`: abre os vídeos persistidos no Supabase.

Uploads são idempotentes por hash de conteúdo para evitar registros/objetos duplicados. Velocidade e espelhamento também estão conectados ao fluxo visual do editor.

## Verificação

Antes de publicar uma mudança:

```text
python -m compileall -q .
python -m unittest discover -s tests -v
```

O repositório também possui GitHub Actions para executar as mesmas validações em Python 3.12.

Os segredos anteriormente publicados ou exibidos em capturas de tela devem ser revogados e substituídos nos provedores.
