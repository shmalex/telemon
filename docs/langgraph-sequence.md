# LangGraph — как это работает (sequence diagram)

```mermaid
sequenceDiagram
    actor Telegram
    participant telemon as telemon.py
    participant graph as LangGraph
    participant state as State (dict)
    participant LLM
    participant disk as alert_history.json

    Telegram->>telemon: алерт сработал
    telemon->>graph: graph.invoke(initial_state)

    Note over graph,state: Граф передаёт state через узлы

    graph->>disk: load_history
    disk-->>state: alert_history = [...]

    graph->>state: collect_context
    state-->>state: context = "CPU 87%, Load 19.4..."

    loop до 4 итераций
        graph->>LLM: decide — что собрать дальше?
        LLM-->>state: "gather_processes"
        graph->>state: gather_processes
        state-->>state: gathered += [("processes", "...")]

        graph->>LLM: decide — достаточно?
        LLM-->>state: "done"
    end

    graph->>LLM: detect_pattern — есть повторения?
    LLM-->>state: pattern_note = "5× за 24h, каждые 30 мин"

    graph->>LLM: analyze — root cause?
    LLM-->>state: analysis = "postgres съедает CPU..."

    graph->>state: format_report
    state-->>state: report = "🔍 Diagnostic report..."

    graph->>disk: save_history
    disk-->>disk: append + trim to 50

    graph-->>telemon: result["report"]
    telemon->>Telegram: sendMessage(report)
```

---

## Ключевые моменты

**State** — это словарь который живёт всё время работы графа.
Каждый узел читает из него что нужно и дописывает своё поле.
Никаких глобальных переменных — всё через state.

**Цикл** — узел `decide` и узлы `gather_*` образуют петлю.
LLM решает куда идти. Когда говорит `"done"` — петля рвётся,
граф идёт дальше.

**Память** — `load_history` и `save_history` — это просто два узла
на краях графа. Один читает файл в state в начале,
другой пишет обратно в конце. LLM видит историю как обычный текст.
```
