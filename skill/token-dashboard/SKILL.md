---
name: token-dashboard
description: Gera ou atualiza um dashboard HTML local com o consumo de tokens do Claude Code (input, output, cache, ferramentas usadas por sessão/solicitação), lendo os transcripts JSONL em ~/.claude/projects/. Use sempre que o usuário pedir para "ver consumo de tokens", "atualizar o dashboard de tokens", "quanto gastei de tokens", "gerar relatório de uso do Claude Code", ou pedir para automatizar isso via hook.
---

# Token Dashboard

Gera um `tokens.html` local a partir dos transcripts do Claude Code, sem chamar nenhuma API externa — só lê os arquivos `.jsonl` que já existem em `~/.claude/projects/`.

## Quando usar

- Usuário pede pra ver/atualizar consumo de tokens do Claude Code
- Usuário pergunta quanto gastou de tokens numa sessão ou projeto
- Usuário pede pra configurar atualização automática do dashboard

## Como executar

**No Claude Code (local, lê direto do disco):**

```bash
python scripts/gerar_dashboard.py --out tokens.html --dias 7
```

**No Claude.ai web (sem acesso ao disco do usuário — precisa de upload):**

O Claude.ai roda em container isolado, sem acesso a `~/.claude/projects` do usuário. Se o usuário pedir o dashboard aqui, ele precisa primeiro subir os arquivos:

1. Pedir ao usuário pra zipar a pasta `~/.claude/projects` inteira (ou selecionar os `.jsonl` que quer analisar) e fazer upload
2. Rodar o script apontando `--input` pro arquivo enviado, que aceita pasta, `.zip` ou `.jsonl` único:

```bash
python scripts/gerar_dashboard.py --input /mnt/user-data/uploads/projects.zip --out /mnt/user-data/outputs/tokens.html --dias 30
```

3. Usar `present_files` pra entregar o `tokens.html` gerado

Parâmetros:
- `--out`: caminho do HTML de saída (padrão `tokens.html`)
- `--dias`: janela de tempo pra considerar sessões (padrão 7)
- `--input`: pasta, `.zip` ou `.jsonl` único com os transcripts (padrão `~/.claude/projects`, só funciona localmente)

## Automatizar com hook (opcional)

Se o usuário quiser que o dashboard atualize sozinho a cada resposta, adicionar um **Stop hook** no `~/.claude/settings.json` (ou settings do projeto) que roda o script depois de cada turno:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python /caminho/completo/para/scripts/gerar_dashboard.py --out /caminho/completo/para/tokens.html --dias 7"
          }
        ]
      }
    ]
  }
}
```

Usar sempre caminho absoluto no `command`, já que hooks não herdam o diretório da skill. Confirmar com o usuário o caminho onde a skill foi instalada antes de escrever o hook.

## Detalhes do parsing (caso precise debugar)

- Cada linha do `.jsonl` é um evento (`type`: `user`, `assistant`, etc)
- Um evento `user` com `tool_result` no conteúdo NÃO é um prompt novo — é retorno de ferramenta. O script filtra isso via `is_real_user_prompt()`.
- Tokens vêm do campo `message.usage` nos eventos `assistant`: `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`.
- Tool calls vêm dos blocos `type: tool_use` dentro de `message.content`.
- Sessões fora da janela `--dias` (baseado no timestamp do último evento) são ignoradas.
