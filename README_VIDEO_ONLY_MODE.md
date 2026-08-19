# Modo Somente Vídeo

O painel do Growth Bot inclui um botão administrativo **🛑 PARAR TUDO • SOMENTE VÍDEO**.

Quando ativado:

- o modo manual é encerrado e seu estado persistido é desligado;
- o APScheduler é pausado;
- operações Instagram passam a ser consideradas pausadas nos checkpoints de segurança;
- comandos e callbacks que não pertencem ao fluxo de vídeo são bloqueados;
- continuam liberados `/download`, biblioteca, lote, fundos, configurações e processamento de vídeo;
- o estado `video_only` é salvo em `bot_state` e reaplicado após reinício;
- no boot em modo somente vídeo, sessões Instagram não são restauradas e o modo manual não é retomado.

O botão **▶️ REATIVAR SISTEMA COMPLETO** volta o scheduler ao funcionamento normal. O modo manual permanece desligado e precisa ser iniciado novamente de forma explícita.

Observação operacional: uma requisição Instagram que já esteja em andamento no exato instante do acionamento pode terminar; novas ações são bloqueadas no próximo checkpoint seguro do fluxo atual.
