# CHANGELOG

本ファイルは [release-please](https://github.com/googleapis/release-please) により自動生成される。
release-please 導入前（〜2026-04-18、v0.3.0 リリース時点）のエントリは [docs/changelog-historical.md](docs/changelog-historical.md) を参照。

---

## [0.9.0](https://github.com/shigechika/jquants-mcp/compare/v0.8.0...v0.9.0) (2026-04-29)


### Features

* **cache:** add Tier 1 cache table for equities_earnings_calendar ([#182](https://github.com/shigechika/jquants-mcp/issues/182)) ([d336edc](https://github.com/shigechika/jquants-mcp/commit/d336edca29526d75bc59697037e05a61a197645c))
* **scripts:** add verify_cache_completeness.py CLI ([#183](https://github.com/shigechika/jquants-mcp/issues/183)) ([c7a4cf6](https://github.com/shigechika/jquants-mcp/commit/c7a4cf6e0eb1c2356a46a6e460e98d515381482e))
* **v0.8:** cache freshness fields in health_check + screener cache_not_ready guard ([#181](https://github.com/shigechika/jquants-mcp/issues/181)) ([6552eac](https://github.com/shigechika/jquants-mcp/commit/6552eaca94bad048606469076739de6dfc2407b3))


### Bug Fixes

* pd.Timestamp.replace()の誤呼び出しを修正（決算発表予定キャッシュ蓄積不全 [#177](https://github.com/shigechika/jquants-mcp/issues/177)） ([#178](https://github.com/shigechika/jquants-mcp/issues/178)) ([f50326c](https://github.com/shigechika/jquants-mcp/commit/f50326ccbc64eff87d0d4d9e5b8ec18627e6f8d4))

## [0.8.0](https://github.com/shigechika/jquants-mcp/compare/v0.7.0...v0.8.0) (2026-04-27)


### Features

* **health_check:** add cache_ready boolean field to response ([#168](https://github.com/shigechika/jquants-mcp/issues/168)) ([8174243](https://github.com/shigechika/jquants-mcp/commit/8174243c90608fc4692258dbe56e6adfbd841d2f))

## [0.7.0](https://github.com/shigechika/jquants-mcp/compare/v0.6.0...v0.7.0) (2026-04-26)


### Features

* **screener:** pre-compute cross-sectional results + multi-date range tools ([#142](https://github.com/shigechika/jquants-mcp/issues/142), [#143](https://github.com/shigechika/jquants-mcp/issues/143)) ([#161](https://github.com/shigechika/jquants-mcp/issues/161)) ([cd5eef3](https://github.com/shigechika/jquants-mcp/commit/cd5eef3923e44c7b9dc900fd8b1ce859ff83e103))
* **screener:** refuse out-of-cache dates with OutOfCacheRange error ([#162](https://github.com/shigechika/jquants-mcp/issues/162)) ([d74bc63](https://github.com/shigechika/jquants-mcp/commit/d74bc639bff8fef86541666470c51096696b47f8))


### Bug Fixes

* **cache:** kick off integrity check at construction time so health_check sees pending/ok ([#157](https://github.com/shigechika/jquants-mcp/issues/157)) ([424d53c](https://github.com/shigechika/jquants-mcp/commit/424d53c004bf4b9290df6cd0307115e487161612))
* **cd:** replace dorny/paths-filter with native git diff to fix HEAD~1 refspec error ([#160](https://github.com/shigechika/jquants-mcp/issues/160)) ([017bc85](https://github.com/shigechika/jquants-mcp/commit/017bc851160078b3022b7f8af2fa36fdd426039d))

## [0.6.0](https://github.com/shigechika/jquants-mcp/compare/v0.5.0...v0.6.0) (2026-04-25)


### Features

* add server.json manifest for MCP registry ([#130](https://github.com/shigechika/jquants-mcp/issues/130)) ([0c20096](https://github.com/shigechika/jquants-mcp/commit/0c20096ebaedcbc1b0adc20247fc174dbd09caea)), closes [#123](https://github.com/shigechika/jquants-mcp/issues/123) [#102](https://github.com/shigechika/jquants-mcp/issues/102)
* **annotations:** declare MCP tool annotations on every registered tool ([#145](https://github.com/shigechika/jquants-mcp/issues/145)) ([2f86d9c](https://github.com/shigechika/jquants-mcp/commit/2f86d9cac726d0218cea73dce47c0d1f45e31712))
* **charts:** add render_candlestick (opt-in [charts] extra) ([#134](https://github.com/shigechika/jquants-mcp/issues/134)) ([a2d1898](https://github.com/shigechika/jquants-mcp/commit/a2d1898d1b98d39776b3948e51ead7c26ad12033))
* **charts:** JP SMA defaults + edge-case tests + addplot=None bug fix ([#147](https://github.com/shigechika/jquants-mcp/issues/147)) ([7c2676a](https://github.com/shigechika/jquants-mcp/commit/7c2676aed97f3f2073d88ff71ff791b1c6b7f030))
* **charts:** render 寄らずストップ高/安 lock days as coloured horizontal bars ([#149](https://github.com/shigechika/jquants-mcp/issues/149)) ([6c0fb7f](https://github.com/shigechika/jquants-mcp/commit/6c0fb7fd03ddecd0830f6e656aa9c515f17656a6))
* **screener:** add 5 offline screener tools (52w + YTD high/low) ([#133](https://github.com/shigechika/jquants-mcp/issues/133)) ([be7db8c](https://github.com/shigechika/jquants-mcp/commit/be7db8c4a5db3dfbcb79e5a9cdb1fbc753259d01))


### Bug Fixes

* **allowlist:** use OAuth email claim, not Google/GitHub sub ([#140](https://github.com/shigechika/jquants-mcp/issues/140)) ([d198901](https://github.com/shigechika/jquants-mcp/commit/d198901f6412d17b70d45a4a19f09ccea3a895da))
* **charts:** collapse alphanumeric ordinary-share codes to 4-char display form ([#152](https://github.com/shigechika/jquants-mcp/issues/152)) ([ef0ff2b](https://github.com/shigechika/jquants-mcp/commit/ef0ff2b6fb20773d1611c869d542bddfa3f67649))
* **docker:** include [charts] extra in Cloud Run image ([#144](https://github.com/shigechika/jquants-mcp/issues/144)) ([1e36f5b](https://github.com/shigechika/jquants-mcp/commit/1e36f5b41dee5aaf36ee4895aec21b016c5e9512))
* **validators:** accept 4-character alphanumeric codes (e.g. 130A) for input symmetry ([#154](https://github.com/shigechika/jquants-mcp/issues/154)) ([b27c18d](https://github.com/shigechika/jquants-mcp/commit/b27c18dc7cd16448d5de8a7ef4e59b86712ea427))
* **validators:** accept alphanumeric J-Quants codes (e.g. 130A0, 554A0) ([#151](https://github.com/shigechika/jquants-mcp/issues/151)) ([1e21cfc](https://github.com/shigechika/jquants-mcp/commit/1e21cfcf01dc5f384821d4793067b206a1eb141e))

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
