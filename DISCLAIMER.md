# Disclaimer / Отказ от ответственности

*English below · [Русская версия](#русская-версия)*

---

## English version

### 1. This is not an official T-Bank product

**tinvest-mcp is an unofficial, independent, community-built client.** It is
developed by the [Plasm](https://plasm.one) team and is **not affiliated
with, endorsed by, certified by, sponsored by, or supported by** T-Bank /
Т-Банк, Т-Технологии, T-Invest / Т-Инвестиции, or any of their affiliates.

* We are not the broker. We hold no licence of any kind.
* T-Bank provides **no support** for this software. Do not open support tickets
  with the broker about this project — open an issue in this repository.
* "T-Bank", "Т-Банк", "Тинькофф", "Tinkoff", "T-Invest" and "Т-Инвестиции" are
  trademarks of their respective owners, used here **descriptively only**, to
  state which public API this software speaks to.
* This project uses T-Bank's **public** [Invest API](https://developer.tbank.ru/invest/intro/intro)
  through their published SDK. It contains no reverse-engineered or private
  interfaces. You are responsible for complying with the broker's API terms of
  use; the broker may rate-limit, restrict, or revoke your API access at their
  own discretion.
* T-Bank also publishes their **own** hosted MCP server. That is a separate,
  first-party product with a different architecture and a different security
  profile. If you want a vendor-supported option, use theirs — see
  [docs/comparison.md](docs/comparison.md) for an honest side-by-side.

### 2. An MCP server is a security boundary, and it carries real risk

This deserves stating plainly, because "install an MCP server" sounds as
harmless as installing a browser extension, and it is not.

An MCP server hands a language model a set of callable tools. When those tools
reach a brokerage account, **the model's output becomes financial action**. The
risks are not hypothetical and are not fully solvable by us:

* **Prompt injection.** Any untrusted text that reaches the model's context —
  a news article, an issuer's description field, a web page, a document, the
  output of another MCP server in the same session — can attempt to instruct
  the model. A model holding trading tools is a model that can be talked into
  using them. This server deliberately exposes **no news, sentiment, or
  external-content tools** for exactly this reason, but it cannot control what
  else your client has loaded.
* **Model error.** LLMs miscount, misread units, and hallucinate confidently.
  The single most expensive mistake in this domain is a bond price: `99.09`
  means 99.09% of face value, not 99 rubles. We put hard numeric checks around
  this, and the checks are not proof of correctness.
* **Tool-surface risk.** Every tool is an attack surface. Read
  [docs/tools.md](docs/tools.md) and know what you have exposed.
* **The protocol has no caller identity.** MCP does not tell the server whether
  a call came from the model or from a human clicking "Confirm". Any client
  that can reach this server can call any registered tool, including execution
  tools. The mitigations that actually work are configuration-level, not
  prompt-level — see [docs/security.md](docs/security.md).
* **Compromise of your machine is compromise of your account.** The token sits
  on your disk. Anything with your user's privileges can read it.

**Corollary:** run in `sandbox` mode first, and run production with a
**read-only token and no execution token at all** unless you have deliberately
decided otherwise. In that configuration no order can be placed even if every
other layer fails, because the execution path has no credential to use.

### 3. Not investment advice

This software is a tool for accessing data and mechanically checking orders. It
is **not** investment advice, a personal recommendation, a financial-analysis
service, or a substitute for a licensed advisor. Nothing it outputs — no
screener ranking, no "target allocation", no yield, no risk level, no analyst
consensus it relays — constitutes a recommendation to buy or sell anything.

Every number it produces is an **estimate** computed from historical data or
relayed from the broker's API: past return, volatility, drawdown, yield, tax
estimates, duration. Estimates are wrong in ways that matter. Taxes in
particular depend on your personal circumstances, and the НДФЛ/ЛДВ figures here
are rough arithmetic, **not** tax advice — consult a qualified professional.

**You make every investment decision. You bear every loss.**

### 4. No warranty, no liability

The software is provided "as is" under the [MIT License](LICENSE), **without
warranty of any kind**. The authors and contributors are not liable for any
claim, damage, or other liability — explicitly including **financial loss,
missed trades, erroneous orders, unintended executions, data loss, or account
restrictions** — arising from the use of this software.

This is software that sends orders to a real broker over a real network, driven
by a non-deterministic model. It has bugs. Assume it has bugs that we have not
found yet.

### 5. Your responsibilities

By running this software you accept that you are responsible for:

1. Reading [docs/security.md](docs/security.md) before connecting a production token.
2. Starting in `sandbox` mode and staying there until you understand the flow.
3. The scope of the token you issue — and never granting the money-transfer scope.
4. The risk limits in your `config.toml`. The shipped defaults are conservative;
   if you raise them, that is your decision.
5. Reviewing every order preview yourself before confirming it.
6. Complying with the broker's API terms and with the tax and securities law of
   your jurisdiction.

---

## Русская версия

### 1. Это не официальный продукт Т-Банка

**tinvest-mcp — неофициальный независимый клиент, созданный сообществом.** Он
разработан командой [Plasm](https://plasm.one) и **не аффилирован с
Т-Банком, не одобрен, не сертифицирован, не спонсирован и не поддерживается**
Т-Банком, Т-Технологиями, Т-Инвестициями или их аффилированными лицами.

* Мы не брокер. У нас нет никаких лицензий.
* Т-Банк **не оказывает поддержку** по этому ПО. Не обращайтесь к брокеру с
  вопросами об этом проекте — создайте issue в этом репозитории.
* «Т-Банк», «Тинькофф», «Т-Инвестиции» — товарные знаки их правообладателей;
  здесь они используются **исключительно описательно**, чтобы указать, с каким
  публичным API работает это ПО.
* Проект использует **публичный** [Invest API](https://developer.tbank.ru/invest/intro/intro)
  через официальный SDK. Никаких приватных или реверс-инженерных интерфейсов
  здесь нет. Соблюдение условий использования API — ваша ответственность;
  брокер вправе ограничить или отозвать ваш доступ по своему усмотрению.
* У Т-Банка есть **свой собственный** размещённый MCP-сервер. Это отдельный
  продукт первой стороны с другой архитектурой и другим профилем безопасности.
  Если вам нужен вариант с поддержкой вендора — используйте их. Честное
  сравнение: [docs/comparison.md](docs/comparison.md).

### 2. MCP-сервер — это граница безопасности, и он несёт реальные риски

Об этом стоит сказать прямо, потому что «поставить MCP-сервер» звучит так же
безобидно, как поставить расширение для браузера. Это не так.

MCP-сервер выдаёт языковой модели набор вызываемых инструментов. Когда эти
инструменты дотягиваются до брокерского счёта, **вывод модели становится
финансовым действием**. Риски не гипотетические и не решаются нами полностью:

* **Prompt injection.** Любой недоверенный текст, попадающий в контекст модели
  — новость, описание эмитента, веб-страница, документ, вывод другого
  MCP-сервера в той же сессии — может попытаться дать модели инструкцию.
  Модель с торговыми инструментами — это модель, которую можно уговорить ими
  воспользоваться. Именно поэтому этот сервер **сознательно не предоставляет
  инструментов для новостей, sentiment или внешнего контента**. Но он не может
  контролировать, что ещё загружено в вашем клиенте.
* **Ошибка модели.** LLM ошибаются в счёте, путают единицы измерения и
  уверенно галлюцинируют. Самая дорогая ошибка в этой области — цена
  облигации: `99.09` означает 99,09% от номинала, а не 99 рублей. Мы обвязали
  это жёсткими численными проверками — и проверки не являются доказательством
  корректности.
* **Риск самой поверхности инструментов.** Каждый инструмент — это поверхность
  атаки. Прочитайте [docs/tools.md](docs/tools.md) и знайте, что вы открыли.
* **В протоколе нет идентичности вызывающего.** MCP не сообщает серверу,
  пришёл вызов от модели или от человека, нажавшего «Подтвердить». Любой
  клиент, дотянувшийся до сервера, может вызвать любой зарегистрированный
  инструмент, включая исполняющие. Работающие меры защиты — на уровне
  конфигурации, а не промпта: см. [docs/security.md](docs/security.md).
* **Компрометация машины = компрометация счёта.** Токен лежит у вас на диске.
  Всё, что работает с правами вашего пользователя, может его прочитать.

**Следствие:** сначала работайте в режиме `sandbox`, а в прод выходите с
**read-only токеном и вообще без токена исполнения**, если вы осознанно не
решили иначе. В такой конфигурации ордер не может быть выставлен, даже если
откажут все остальные слои защиты — исполняющему пути просто нечем
авторизоваться.

### 3. Это не инвестиционная рекомендация

Это ПО — инструмент доступа к данным и механической проверки заявок. Оно
**не** является инвестиционной рекомендацией, индивидуальной инвестиционной
рекомендацией, услугой финансового анализа или заменой лицензированному
консультанту. Ничто из того, что оно выводит — ранжирование скринера,
«целевая аллокация», доходность, уровень риска, консенсус аналитиков — не
является рекомендацией покупать или продавать что-либо.

Каждое число, которое оно выдаёт, — это **оценка**, рассчитанная по
историческим данным или полученная из API брокера: прошлая доходность,
волатильность, просадка, доходность к погашению, оценки налогов, дюрация.
Оценки ошибаются, и ошибаются существенно. Налоги особенно зависят от вашей
личной ситуации: расчёты НДФЛ и ЛДВ здесь — грубая арифметика, а **не**
налоговая консультация. Обратитесь к квалифицированному специалисту.

**Все инвестиционные решения принимаете вы. Все убытки несёте вы.**

### 4. Без гарантий и без ответственности

ПО предоставляется «как есть» по [лицензии MIT](LICENSE), **без каких-либо
гарантий**. Авторы и контрибьюторы не несут ответственности по любым
претензиям, убыткам и иным требованиям — включая прямо **финансовые потери,
упущенные сделки, ошибочные заявки, непреднамеренное исполнение, потерю
данных и ограничения по счёту** — возникшим из использования этого ПО.

Это программа, которая отправляет заявки реальному брокеру по реальной сети
под управлением недетерминированной модели. В ней есть баги. Считайте, что в
ней есть баги, которых мы ещё не нашли.

### 5. Ваша ответственность

Запуская это ПО, вы принимаете, что отвечаете за:

1. Прочтение [docs/security.md](docs/security.md) до подключения
   продового токена.
2. Старт в режиме `sandbox` и работу в нём, пока не разберётесь в процессе.
3. Выбранный скоуп токена — и за то, чтобы **никогда** не выдавать скоуп на
   переводы денег.
4. Значения риск-лимитов в вашем `config.toml`. Дефолты консервативны; если
   вы их повышаете — это ваше решение.
5. Самостоятельную проверку каждого превью заявки перед подтверждением.
6. Соблюдение условий API брокера, а также налогового и финансового
   законодательства вашей юрисдикции.

---

Built by [Plasm](https://plasm.one) · Сделано командой [Plasm](https://plasm.one)
