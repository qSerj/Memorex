# Synthetic Wiki-first benchmark

All people, organizations, products, events, and decisions in this directory are fictional. The
fixture tests semantic ingest without publishing the private corpus used during discovery.

`baseline/` is ingested first. After reviewing and freezing that Wiki, `incremental/` is added to
test whether existing pages evolve instead of receiving an isolated document summary. Candidate
goldens belong under `goldens/<runner>/{baseline,incremental}/` and must record the runner, model,
CLI version, prompt version, and source checksums in their manifest.

Questions for the read-only query probe:

1. Почему команда сохранила «Контур» ядром, но всё же рассматривает внешние надстройки?
2. Чем комплект отличается от сборки и где возникает проблема с чеком?
3. Где в пути заказа повторяется ручной ввод?
4. Что изменилось после пилота мобильной приёмки?
5. Какие утверждения всё ещё нельзя считать подтверждёнными?
