# Growth Bot

Bot privado para Telegram que centraliza contas e campanhas do Instagram e integra um editor de vídeo hospedado no Railway.

## Componentes

- painel e fluxos guiados no Telegram;
- autenticação Instagram pelo fluxo CAA/Bloks atual do `instagrapi`;
- confirmação de código/2FA pelo chat privado do bot;
- persistência de contas, campanhas, listas, logs e backups no Supabase;
- agendador de follow, unfollow, aquecimento, backup e anomalias;
- integração com a Growth Bot Video API.
- editor visual que permite arrastar e redimensionar o vídeo com mouse ou dedo.

## Instalação local (Windows)

Requer Python 3.12. O Python 3.14 ainda não possui wheels compatíveis para todas as dependências deste projeto.

```bat
py -3.12 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env
python main.py
```

Preencha o `.env` local antes de iniciar. No Discloud, cadastre as mesmas chaves como variáveis de ambiente. O arquivo `.env` nunca deve ser enviado ao GitHub.

## Variáveis

Obrigatórias:

- `TELEGRAM_TOKEN` e `TELEGRAM_OWNER_ID`;
- `SUPABASE_URL` e `SUPABASE_KEY`;
- `SESSION_ENCRYPTION_KEY`.

Necessárias para o editor:

- `VIDEO_API_URL` e `VIDEO_API_SECRET`.

Opcionais para a identidade da sessão Instagram:

- `INSTAGRAM_COUNTRY`, `INSTAGRAM_COUNTRY_CODE`, `INSTAGRAM_LOCALE` e `INSTAGRAM_TIMEZONE_OFFSET`;
- `INSTAGRAM_PROXY`, somente quando uma proxy for realmente necessária;
- `SESSIONS_DIR` e `VIDEO_CONFIG_PATH` para alterar os diretórios locais.

Consulte [.env.example](.env.example) para os formatos.

Para um banco Supabase que já existia antes desta revisão, execute uma vez o arquivo
`database/migration_2026_08_14.sql` no SQL Editor. Ele adiciona as colunas usadas pela
regra de unfollow e pelo relatório automático.

## Segurança

- Use o bot apenas em conversa privada e restrinja o acesso pelo painel de usuários.
- Senhas, tokens e códigos de verificação não devem aparecer em commits ou logs.
- Os logs internos de HTTP são silenciados porque a URL da Bot API inclui o token.
- Trocar `SESSION_ENCRYPTION_KEY` invalida dados cifrados com a chave anterior; planeje a recadastração das contas.

## Verificação

```bat
python -m compileall -q .
python -m unittest discover -s tests -v
python -m pip check
```

As automações de Instagram devem respeitar as regras da plataforma e ser usadas somente em contas que você administra.
