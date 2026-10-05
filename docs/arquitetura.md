# Arquitetura

Visão técnica da ManaTime para quem vai manter ou estender o código. Para instalar e usar, veja o
[README](../README.md).

## Componentes

| Peça | Arquivo | Papel |
|---|---|---|
| Tela virtual + VNC | `entrypoint.sh` | `Xvnc :99` é a tela dos Chromium e o servidor VNC; `websockify` expõe o noVNC na porta 6080 |
| API e agendamento | `app/main.py` | FastAPI: rotas do painel, leitura por intervalo, horas por empresa, limpeza do cache, atualização automática |
| Navegadores | `app/browser.py` | um `AccountBrowser` por conta (Playwright, perfil persistente), aberto só durante a leitura |
| Leitor | `app/parser.py` | converte o JSON do Outlook e as descrições do Google em eventos; junta convites encaminhados |
| Plano | `app/plan.py` | o que é seu e não vem das agendas: "não vou", atividades e tarefas (`data/plan.json`) |
| Configuração | `app/config.py` | variáveis de ambiente, contas (`config/accounts.yaml`) e utilitários de datas |
| Painel | `app/static/` | `index.html`, `app.css`, `app.js` (sem build, sem framework) |

## Leitura das agendas

`refresh_range(start, end)` lê o mesmo intervalo de todas as contas em paralelo e grava um
arquivo por dia em `data/cache/AAAA-MM-DD.json`. Cada conta é lida de um jeito:

### Microsoft 365 e Outlook pessoal

O Outlook ignora a data da URL e sempre carrega a semana atual. O agente intercepta o pedido
`service.svc?action=GetCalendarView` que a própria página faz e reescreve `RangeStart` e
`RangeEnd` (no cabeçalho `x-owa-urlpostdata`) com o intervalo desejado. A resposta JSON é
percorrida atrás de objetos com `Subject`, `Start` e `End` (formatos OWA e Graph).

Se o Outlook abrir sem pedir os eventos, o agente tenta mais uma vez. Se continuar sem dados, a
conta fica com erro `calendar_no_data` e as últimas reuniões boas são mantidas, em vez de mostrar
um dia livre falso.

### Google

Cada evento na visão da semana tem uma descrição para leitores de tela, por exemplo
`10:00 até 11:00, Título, Pessoa, Aceito, Local: Sala 3, 4 de outubro de 2026`. O leitor divide
por vírgulas e classifica cada pedaço (horário, data, status, local, organizador, título). O
cabeçalho `Semana de …, N eventos` serve de conferência: se o leitor achar menos eventos que N, a
conta mostra um aviso.

### Sessão expirada

Se a página cair num endereço de login (`accounts.google.com`, `login.microsoftonline.com`…) ou na
página de produto do Google, a conta fica com status `login_required`.

### Recursos

Cada Chromium fecha `BROWSER_IDLE_SECONDS` depois da última leitura e reabre na próxima, com o
mesmo perfil. Na tela de login, ele fica aberto por 30 minutos. O cache de disco de cada perfil é
limitado a 40 MB e os dias com mais de `CACHE_DAYS` são apagados uma vez por dia.

## Modelo de dados

### Evento (cache)

```json
{"title": "…", "start": "09:00", "end": "09:30", "all_day": false, "location": "Teams",
 "meeting_url": null, "organizer": "…", "response": "accepted|tentative|unknown|declined"}
```

Por dia e por conta, o cache guarda `status` (`ok`, `login_required`, `error`), `updated_at`,
`events` e, quando a leitura falha, `stale: true` com as últimas reuniões boas.

### Plano (`data/plan.json`)

```json
{
  "skipped": {"2026-10-05": ["<conta>|<início>|<título>"]},
  "activities": [{"id": "…", "date": "2026-10-05", "start": "12:00", "end": "13:00", "title": "Almoço"}],
  "tasks": [{"id": "…", "date": "2026-10-05" , "time": "11:30", "title": "…", "tags": ["cliente"], "done": false}]
}
```

A chave de uma reunião é `conta|início|título`, a mesma no servidor e no painel. Reuniões em
`skipped` saem das horas, dos conflitos e do tempo livre. Gravações são atômicas
(arquivo temporário + `os.replace`).

## API

| Método | Rota | |
|---|---|---|
| GET | `/api/agenda?date=` | um dia, com o plano |
| GET | `/api/week?date=` | a semana do dia, com horas da semana e do mês |
| POST | `/api/refresh?date=&span=week\|month` | relê as agendas agora |
| GET | `/api/config` | porta do noVNC |
| POST | `/api/plan/skip` | marca ou desmarca "não vou" |
| POST / DELETE | `/api/plan/activities[/{id}]` | atividades no tempo livre |
| GET | `/api/todo` | tarefas agrupadas |
| POST / PATCH / DELETE | `/api/plan/tasks[/{id}]` | tarefas (título, data, horário, tags, feita) |
| GET / POST | `/api/accounts` | lista e cria contas |
| PATCH / DELETE | `/api/accounts/{id}` | edita ou remove (`?forget_login=true` apaga o perfil) |
| POST | `/api/accounts/{id}/move?step=-1\|1` | reordena |
| POST | `/api/accounts/{id}/login` | abre a janela de login no noVNC |

## Painel

- Um único `app.js` sem framework. O estado de cada seção vem da API; preferências ficam no
  `localStorage` (`agenda.view`, `mana.theme`, `mana.pomo.*`, `agenda.remind`).
- O tema é aplicado por um script no `<head>`, antes da pintura, para não piscar.
- O pomodoro guarda o horário de término, não um contador; por isso continua certo com a aba em
  segundo plano ou recarregada.
- Cores: azul-marinho para a interface, verde-água para "você" (agora, hoje, atividades), laranja
  para conflitos. As cores das contas foram validadas para daltonismo e contraste nos dois temas.

## Testes

- `tests/test_*.py`: leitor, plano, contas e totais, com FastAPI `TestClient` e arquivos temporários.
- `tests/ui/`: Playwright contra `fake_api.py`, que serve os arquivos estáticos e simula a API com
  empresas fictícias e o relógio congelado em 05/10/2026 às 10:15.
