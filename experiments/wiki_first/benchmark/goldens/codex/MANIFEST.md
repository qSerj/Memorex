# Codex synthetic golden manifest

- Generated: 2026-08-21
- Runner: Codex CLI `0.149.0`
- Model: `gpt-5.6-sol`
- Reasoning effort: `max`
- Prompt: `wiki-first-ingest-v1`
- Manual edits after generation: none

## Baseline run

- duration: 457,180 ms;
- input tokens: 512,642;
- cached input tokens: 463,360;
- output tokens: 22,542;
- snapshot tree: `e69f3d8b1b208616c41b52f9d9323555c370f2e75ec852bf551956b9b1e2e789`;
- deterministic validation: pass, 4 Markdown files, 20,225 bytes;
- review warnings: expected growth warnings from the empty initial Wiki.

## Incremental run

- duration: 719,063 ms;
- input tokens: 1,637,270;
- cached input tokens: 1,511,680;
- output tokens: 34,317;
- snapshot tree: `74c9267850aea542e4d6710ce24a1a92a0dc703ca58d0c4eba58439d7ae5a562`;
- deterministic validation: pass, 4 Markdown files, 29,435 bytes;
- review warnings: none;
- new per-source page created: no; all three thematic pages and the landing page were updated.

## Source SHA-256

```text
0000be02495d1ea01d2aadd0b0dc385fb594f9e73ca1af76581cb613ff96678f  01-architecture-meeting.txt
78cc4cf813aab574124e326ba9f658ab00dbbd47f07bd5ceaff048070bed42f0  02-order-flow.md
35ab65fd9af30115904ae71bb322b17e77cfa6cdb734c7de3045833cf6a81c1f  03-kits-discussion.txt
3176c1815fbc6befdb618b6b7315d75d094d5dc304d3edbd7f5758cf96f6f6d3  04-receiving-pilot.txt
4a18b80477cb01049de28bb4b030394f5394a29bf27ee5ac35140ff970a06f63  05-device-idea.md
57f5d65c87ab5641eb61e8d89812ba86c299b332a73e25dfaba08f45532a373c  06-pilot-followup.md
```

## Query smoke

Question: `Что изменилось после пилота мобильной приёмки?`

- duration: 67,515 ms;
- input tokens: 102,603;
- cached input tokens: 76,544;
- output tokens: 1,646;
- Wiki pages visited: `warehouse-flows`, `solution-boundaries`;
- raw fallback: none;
- answer: `incremental/query-answer.md`.

## Output SHA-256

```text
63396ad37dab4824711afd6d6445d90a835eba34b25571add22592aa2a35613d  baseline/wiki/README.md
b17afaf5019f98deb63ac8bd989e71a9181c62f5609f5761199c1bef15b769a3  baseline/wiki/kits-and-assemblies.md
2d5f6a4681245ebcf52441635dc9b829dc06ff4c0ecd1288f61a6880ab715661  baseline/wiki/solution-boundaries.md
295ab88c44178510c0d9a039b4e841e1d350416ed4cfcaddffabd44a6cfaaf5d  baseline/wiki/warehouse-flows.md
dd94cb0b30e52b778077cd227521cc29c3ee53b507a599143aa31b0c551fb004  baseline/proposal-report.md
e0c495428f4d28fab68b576d7097ec74b9d8e0322817ce7dad885897a565b8cb  incremental/wiki/README.md
1406521bb450066b08ba62a59cccb899bbdb0018daa2cedfc54e617e74a18045  incremental/wiki/kits-and-assemblies.md
3a63313014524417c9fd50b9d8c56f9f07b424cdd1eec4e499ef81af4de07144  incremental/wiki/solution-boundaries.md
541b9186a6a3d73d7daaf0ebd541eb38ccd020f47761e68257dd1fd46ad228f3  incremental/wiki/warehouse-flows.md
ebef0f2e525e5e2e70751ba2863aaad93a63bda0103b153e1c9753e67180b762  incremental/proposal-report.md
780593dc6e726ec7df0cac9a9e4ce91093717034491577f65bdddd066ba5c25e  incremental/query-answer.md
```
