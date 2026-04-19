# CHANGELOG

本ファイルは [release-please](https://github.com/googleapis/release-please) により自動生成される。
release-please 導入前（〜2026-04-18、v0.3.0 リリース時点）のエントリは [docs/changelog-historical.md](docs/changelog-historical.md) を参照。

---

## [0.5.0](https://github.com/shigechika/jquants-mcp/compare/v0.4.0...v0.5.0) (2026-04-19)


### Features

* **config:** honour JQUANTS_API_TOML_PATH to dodge macOS 26 launchd sandbox ([#128](https://github.com/shigechika/jquants-mcp/issues/128)) ([33b82b3](https://github.com/shigechika/jquants-mcp/commit/33b82b3b0606906c3614dd1043665f513f346e7c)), closes [#102](https://github.com/shigechika/jquants-mcp/issues/102)

## [0.4.0](https://github.com/shigechika/jquants-mcp/compare/v0.3.0...v0.4.0) (2026-04-19)


### Features

* add JQUANTS_ALLOWED_EMAILS allowlist for multi-user access ([#121](https://github.com/shigechika/jquants-mcp/issues/121)) ([77ead63](https://github.com/shigechika/jquants-mcp/commit/77ead635a76eaae11d2de0e224c86ff514b94af7)), closes [#107](https://github.com/shigechika/jquants-mcp/issues/107) [#102](https://github.com/shigechika/jquants-mcp/issues/102)

## [0.3.0](https://github.com/shigechika/jquants-mcp/compare/v0.2.0...v0.3.0) (2026-04-18)


### ⚠ BREAKING CHANGES

* pip install target is now "jquants-mcp" and the CLI entry point is "jquants-mcp". Import path is now "from jquants_mcp import ...".

### Features

* rename package to jquants-mcp ([#116](https://github.com/shigechika/jquants-mcp/issues/116)) ([cb365fa](https://github.com/shigechika/jquants-mcp/commit/cb365faf9d513a4ac7830f835378ccd7a6f73932)), closes [#105](https://github.com/shigechika/jquants-mcp/issues/105)
