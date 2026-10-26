# Changelog

## 0.1.0 (2026-10-26)


### Features

* act on the agent buyer panel: convert at $0.02, quantum off by default ([10e827d](https://github.com/balogvn/aimart/commit/10e827d230678e29b3946ce65f2335255b611648))
* CI/CD, Cloud Run deploy, docs, full test suite; fix review findings ([7f98b51](https://github.com/balogvn/aimart/commit/7f98b510ed71aba1fd1e63443fb3e037c7c12c30))
* conversion engine, x402 payments, MCP and REST server ([40b6380](https://github.com/balogvn/aimart/commit/40b638096b4ac9768b823b84fd6409d73b5f8f49))
* PDF tables, demand signals, multi-file joins, published data retention ([0bf5726](https://github.com/balogvn/aimart/commit/0bf5726bbda0be8d02445216f46b0cb17127673c))
* premium quantum tier and post-quantum provenance ([d909ef1](https://github.com/balogvn/aimart/commit/d909ef12a1c9bc08e3bf49e084af45183e7bca93))


### Bug fixes

* a bodiless upload probe is quoted; evicted GCS cache blobs expire at the TTL ([2ef4ef5](https://github.com/balogvn/aimart/commit/2ef4ef5448a584ae56a338c1db86cafc18b6886f))
* a cancelled MCP settlement deletes the output it withheld ([20dedf5](https://github.com/balogvn/aimart/commit/20dedf5eacbee4de2c3fd2c1aaa35727c0e42f7c))
* check the upload form before quoting it ([2d1edc5](https://github.com/balogvn/aimart/commit/2d1edc57accf1679600ea501e66fd3762d7f9a6c))
* committing a settled call's outputs never fails the call ([34c7b78](https://github.com/balogvn/aimart/commit/34c7b788ce3af104db950d43b5eeb828685fb9d8))
* **convert:** a column declared string keeps its text ([4ec6495](https://github.com/balogvn/aimart/commit/4ec64959c6b12e852b3a458b62d1958faa074547))
* **convert:** a decimal cast that would round is a counted reject ([9d921ec](https://github.com/balogvn/aimart/commit/9d921eca41ebe05f83874df1f3b8bcb815e66ae2))
* **convert:** a decimal read from an inferred number keeps its nulls and notes ([3e60862](https://github.com/balogvn/aimart/commit/3e608627ba2107c6fcfb7e3a454f76136c3d7d20))
* **convert:** a number cast to bool must be 0 or 1 ([3f39784](https://github.com/balogvn/aimart/commit/3f39784457d4b14af8dc73054f2421d8ee0a643b))
* **convert:** say what a typed timestamp cast assumed about zones ([9c8fb71](https://github.com/balogvn/aimart/commit/9c8fb71130e2cb8ed2466c92bd23beaef7b80f9f))
* **convert:** say when a timestamptz cast assumed UTC for naive values ([6ed357d](https://github.com/balogvn/aimart/commit/6ed357d3c224546e1b2cc3c2194b77dd7f0eb991))
* copy an upload to disk only once it holds a job slot ([5f984a5](https://github.com/balogvn/aimart/commit/5f984a581e24245aefcdb69f8e7d23ee70ef4cba))
* delete withheld outputs; quote only served, valid calls ([77a3ff2](https://github.com/balogvn/aimart/commit/77a3ff2e0dc9d24f391af0dce3fbc392a1194e0d))
* **delimited:** skip the blank lines the sniffer skipped before the header ([969e070](https://github.com/balogvn/aimart/commit/969e07045e9bb1d44879df6f35dda983232919da))
* **deploy:** accept cross-project secret paths; print every variable in the gh block ([933af4c](https://github.com/balogvn/aimart/commit/933af4c74ba456e990b2d00d2424e393f9b30af4))
* **deploy:** pass AIMART_JOB_MEMORY_LIMIT_MB from a GitHub variable ([39100b3](https://github.com/balogvn/aimart/commit/39100b325f29d0ee896b129d09222135c34d83cc))
* **detect:** a PDF with a BOM or junk before its header is a PDF, not CSV ([21ec054](https://github.com/balogvn/aimart/commit/21ec05409247d8152f303461ca621fd22a8653a1))
* **infer:** a zone name no longer shifts the naive values after it ([2b5ede7](https://github.com/balogvn/aimart/commit/2b5ede73b60d5448671bcc7e4a425de269620026))
* **infer:** an expected_schema cast decides the number convention per column ([9f1319e](https://github.com/balogvn/aimart/commit/9f1319e98da303d13b4f6c3db70ff788a2b4563e))
* **infer:** never charge for numbers or times read in the wrong convention ([91d4afa](https://github.com/balogvn/aimart/commit/91d4afa4088aef6d10251821ec81c35cbed8bda5))
* **infer:** report what expected_schema casts rejected and assumed ([57041ed](https://github.com/balogvn/aimart/commit/57041eddceb371060e8f0357778954b527dbed67))
* **inspect:** a range Python cannot hold is unknown, not a 500 ([d77da36](https://github.com/balogvn/aimart/commit/d77da36086ea57345f69b7a419a886b217785c9d))
* **isolate:** cap each isolated job's memory with an RSS watchdog ([9bdf17b](https://github.com/balogvn/aimart/commit/9bdf17b97533b37060b81770ac62dcfafd2fbe82))
* **isolate:** default the job memory cap to the configured setting ([88e1804](https://github.com/balogvn/aimart/commit/88e18040f55c05f4aead3cda96a936907011badd))
* **json:** keep nested keys that differ only by letter case ([ebfef59](https://github.com/balogvn/aimart/commit/ebfef591f200a2d46eeaff0baa908f79641d5e27))
* **json:** rename case-variant keys only where DuckDB reads a STRUCT ([48c7021](https://github.com/balogvn/aimart/commit/48c702127ecd99d10da417b1bca7b4d1070d3c5f))
* never charge for a dead artifact; delete expired GCS blobs; cap call inputs ([9a6e6bf](https://github.com/balogvn/aimart/commit/9a6e6bf00f872be9fa79c62a7f84cdd2249c56dd))
* never evict what a call is reading, a fresh output, or an unsettled one ([aa2cd57](https://github.com/balogvn/aimart/commit/aa2cd572e250e749ce803b65259c3011c8f00dca))
* **pdf:** bound form XObject recursion and page count; never leak the PDF's file ([3bb0aeb](https://github.com/balogvn/aimart/commit/3bb0aeb66f8ba49f00970fc1a712ccb191749fdd))
* **pdf:** bound hostile PDFs and fix header, fallback and continuation errors ([46954d7](https://github.com/balogvn/aimart/commit/46954d7fb5320fa48c6b718e87832b458889d04a))
* **pdf:** charge the table search to the page and document budget ([a955147](https://github.com/balogvn/aimart/commit/a955147d02a489fb7cca45863d07169e093e3796))
* **pdf:** keep a blank-corner header and year headers; search borderless tables only when no ruled one ([7f91ab2](https://github.com/balogvn/aimart/commit/7f91ab236984ca729a1fd2045c7a5718f3a349e8))
* **pdf:** pick the header by the columns it names; merge a one-row spill ([c47908c](https://github.com/balogvn/aimart/commit/c47908c9bc4b1c5eb146cb895951df83d2a596bc))
* **pdf:** say borderless tables are read only when no page has a ruled one ([c4be891](https://github.com/balogvn/aimart/commit/c4be89155c00e4138f0907273bca497bc303f51a))
* **pdf:** stop path-by-path walks over shared objects ([354b5c9](https://github.com/balogvn/aimart/commit/354b5c94545f1216649845263e9bd7fd4b7aa602))
* **tabular:** bound preview decode and nested results, type unregistrable inputs ([bc36832](https://github.com/balogvn/aimart/commit/bc368322152aba55bc1a6cba9f49eeb2b05f1d8c))
* **tabular:** cap preview batches that hold text; cover render fallbacks ([683a64b](https://github.com/balogvn/aimart/commit/683a64b4269d72dc3455e89f789f6a4bfb8b77a5))
* **tabular:** keep the Arrow scan for column chunks DuckDB reads whole ([1a0d29b](https://github.com/balogvn/aimart/commit/1a0d29b13192c3a2cbb17368be895325797b2d15))
* **tabular:** positional rows, bounded result memory, typed non-Parquet errors ([66a500e](https://github.com/balogvn/aimart/commit/66a500e53a867029e640035d1ae684e34650c0d3))
* **tabular:** scan query inputs with DuckDB's Parquet reader; cut long map keys ([18e2e21](https://github.com/balogvn/aimart/commit/18e2e210ceacce0ebe59acb9d2924cc6197e2f25))
* **tabular:** type Arrow errors from a query; no server path in preview errors ([5f69ab5](https://github.com/balogvn/aimart/commit/5f69ab5275271b13e64f1fdae8bf7bc34865d736))
* the query sandbox no longer reveals the server's home directory ([6a0851f](https://github.com/balogvn/aimart/commit/6a0851f7b7e8ff48f16a401a3108482d572deb54))
* track a paid call's outputs only when there is a store to settle them in ([f6f5e2b](https://github.com/balogvn/aimart/commit/f6f5e2bd11be744ca37c9ad2eecbfdb41218ca50))
* wire the review fixes together across areas ([e8d228e](https://github.com/balogvn/aimart/commit/e8d228e78210f6ee79102a0cc2340e3433f4854d))


### Performance

* **json:** match record keys to columns without a second structure pass ([2c5bb62](https://github.com/balogvn/aimart/commit/2c5bb62778ea141495668562800ab6bcb7fc8c62))


### Documentation

* **agents:** fold the review fixes into the code map and guarded bug classes ([725e95a](https://github.com/balogvn/aimart/commit/725e95a2d76c852f8d391d1eaf47ce76c967418c))
* bring security, payments and ADR 0007 up to the merged fixes ([b58b8b2](https://github.com/balogvn/aimart/commit/b58b8b28e4d537942f857dbd16f376cf6c99d19d))
* expires_at is when an artifact becomes unreachable, not when it is deleted ([e46be9d](https://github.com/balogvn/aimart/commit/e46be9df7f6036693c80535155c41a6c05516bc8))
* **infer:** record the column-level convention and the no-rounding rule ([3859c84](https://github.com/balogvn/aimart/commit/3859c847112ec8c17e9da080389e7cdced64396b))
* joined tables take a Parquet url; quantum serving needs the switch too ([8197c71](https://github.com/balogvn/aimart/commit/8197c71778fed85d944eb961939f1565500ea138))
* list the new refusals and report notes for callers ([71136ac](https://github.com/balogvn/aimart/commit/71136ace24ef4ffbae92bc1dd473bd197b3a8c8c))
* PDF tables, joins, demand signals, privacy policy ([6f99145](https://github.com/balogvn/aimart/commit/6f991453274456eb9af4e9648293913e3546ed46))
* **pdf:** clarify where the year-header rule came from ([a539beb](https://github.com/balogvn/aimart/commit/a539bebed958e3bf4b5608e9e6f19fcac20f779f))
* **privacy:** state where inputs live and when outputs go, as the code does ([056f4e3](https://github.com/balogvn/aimart/commit/056f4e3ff57d77d5c4dc16672375af8088483bd2))
* provisional outputs count toward the budget ([e8f02d0](https://github.com/balogvn/aimart/commit/e8f02d09b3742e134bab08830160034554a96235))
* published text matches the code (retention, joins, PDFs, prices, deploy config) ([e22e4d9](https://github.com/balogvn/aimart/commit/e22e4d95ee9ac3684c7d2a359bd82ffc7e217cf8))
* record the per-job memory cap and what it costs ([639a0a8](https://github.com/balogvn/aimart/commit/639a0a874b74d2cfbe7eb8a4a4eae763454fe33e))
* the local server caps job memory too; an unpaid form is a price probe ([d099a19](https://github.com/balogvn/aimart/commit/d099a19a816208fa5a15c36f44eba56575921629))

## Changelog

Release notes are generated by release-please from Conventional Commit messages. Do not edit by hand.
