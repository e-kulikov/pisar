# Changelog

## [0.4.0](https://github.com/e-kulikov/pisar/compare/v0.3.0...v0.4.0) (2026-10-08)


### ⚠ BREAKING CHANGES

* --scope personal|work|all is removed.
* bare space ids and `wiki:space:doc` references are no longer accepted; use `domain/id` addresses and `wiki:domain/id:doc`.

### Features

* add domain and space commands ([db2d2c9](https://github.com/e-kulikov/pisar/commit/db2d2c973287119437eb0d0c8a76154a879bac42))
* add pisar lesson for reviewed cross-domain notes ([757eaed](https://github.com/e-kulikov/pisar/commit/757eaedac04ec4f20af54234b1348031e9802144))
* add pisar research through an isolated web-only claude ([e864afd](https://github.com/e-kulikov/pisar/commit/e864afd19c1b071899660d57c84c0e1e517d3355))
* address spaces as domain/id with per-domain unique ids ([6b5d137](https://github.com/e-kulikov/pisar/commit/6b5d13705ae38029546a759ed10e9eab2f676ae3))
* **agent:** default agent, model and effort from the config file ([c97cd12](https://github.com/e-kulikov/pisar/commit/c97cd12027bcda1eeb8e82d8412ee066d064b12d))
* allow agreed tasks in spaces of any kind ([d318cf3](https://github.com/e-kulikov/pisar/commit/d318cf3936a0692dac5585662e3bc804da6bd07e))
* bundle a Claude plugin and extend the agent launcher ([43ef924](https://github.com/e-kulikov/pisar/commit/43ef924ca9400d5baad28cc9074b9ed2b6797aab))
* **config:** read settings from a config file and add `pisar config show` ([ba20690](https://github.com/e-kulikov/pisar/commit/ba2069023b46d624e747af2d73d7ded8a763a863))
* discover domains by their .domain.toml marker ([b832b81](https://github.com/e-kulikov/pisar/commit/b832b8100deb81562caf7b8fccf5b4d32ee3dad7))
* **guard:** add pisar guard check with domain terms ([9e4c90a](https://github.com/e-kulikov/pisar/commit/9e4c90af2042869742767059eda1d56c3330da25))
* **guard:** report possibly sensitive text with a lexical scan ([948ccd0](https://github.com/e-kulikov/pisar/commit/948ccd0a0f7a4cfaf832fb2151b7864e97ad0d80))
* print revision and sha256 as labelled fields in lesson show ([6da9567](https://github.com/e-kulikov/pisar/commit/6da9567f6b895ca51f4f5edcc4111d99dcdd0d09))
* select domains with --include/--exclude instead of --scope ([3f5dab9](https://github.com/e-kulikov/pisar/commit/3f5dab960105be7adfd9b9340ac1821c533a7097))


### Bug Fixes

* align the lessons skill with the lesson command output fields ([4483aad](https://github.com/e-kulikov/pisar/commit/4483aad9bcbd8e9166e2be1235e043bdb6f7e06b))
* apply one deadline to research stdin, execution and output draining ([84b4937](https://github.com/e-kulikov/pisar/commit/84b49373857e5cf16f20dad9d6ad5f2bc799b65b))
* bound research subprocess output, strings, counts and record size ([c8ba517](https://github.com/e-kulikov/pisar/commit/c8ba517b8b689b4a03c18e1e93ee961fa2e06acd))
* build acceptance commits from an isolated index ([6d9e024](https://github.com/e-kulikov/pisar/commit/6d9e0242c2131cc41005c5b7225aeda1c67fdf1e))
* choose and validate the research working-directory base itself ([effca2b](https://github.com/e-kulikov/pisar/commit/effca2be64d70fbdbda5188455187f78b6dad1d4))
* commit the journaled bytes, never the reread working file ([247f3dc](https://github.com/e-kulikov/pisar/commit/247f3dc702cb2d759c8482ebbc6a2ee6a557d1e5))
* commit the marker an interrupted domain add already wrote ([80a8d20](https://github.com/e-kulikov/pisar/commit/80a8d2094e20d7614541d95c942fc842c23c90b0))
* define the lesson acceptance commit point and persist its decision atomically ([45fe27c](https://github.com/e-kulikov/pisar/commit/45fe27c6a58484bf0061ab0c4e5d085e84082a95))
* diagnose operation state left by pisar 0.3 instead of mis-resuming it ([740d189](https://github.com/e-kulikov/pisar/commit/740d189711106683e1de2bfa69e6fac345703d97))
* finish the index synchronization when a commit is recovered ([6c18048](https://github.com/e-kulikov/pisar/commit/6c18048448ed80250d99d2bd2ca1afdf113ffd39))
* give the agent the tools its plugin workflow needs ([9023b41](https://github.com/e-kulikov/pisar/commit/9023b41a4ce23add7ea93d1d1cc30a194c51d08f))
* give the child repository a synthetic identity in its regression ([bda6e5c](https://github.com/e-kulikov/pisar/commit/bda6e5cea8d164010795a1717f70dd4f208c184e))
* give triage-specific abandonment steps for a pisar 0.3 batch ([315048a](https://github.com/e-kulikov/pisar/commit/315048afef9c408a0eed78b4d76d9c7bba263aac))
* **guard:** detect hostnames regardless of case ([9ba5e18](https://github.com/e-kulikov/pisar/commit/9ba5e184cfd8c0dd47d55cf78850b6fbc9987346))
* **guard:** report every valid IPv6 address ([0553e55](https://github.com/e-kulikov/pisar/commit/0553e55405f9f82d1e25d5925e4cbbbcd914b2b5))
* keep archive upkeep from failing a finalized lesson acceptance ([befea15](https://github.com/e-kulikov/pisar/commit/befea159bbf0ac93d520dc6e9db7866681dfbe02))
* keep bounded private diagnostics and print only sanitised research errors ([5ab3fd9](https://github.com/e-kulikov/pisar/commit/5ab3fd9c55cad27f8b64ff952fe16fd6f2b5be09))
* keep capture journals of distinct addresses apart ([6a96605](https://github.com/e-kulikov/pisar/commit/6a9660577f1f76ef4f3b99fce341985c736f060e))
* keep the lesson workspace while its acceptance is incomplete ([2873f04](https://github.com/e-kulikov/pisar/commit/2873f04579d482875389514d0ce498e4dbc9f390))
* let selected reads ignore problems in unselected domains ([5de15b2](https://github.com/e-kulikov/pisar/commit/5de15b2aff71bba6346b206b1c6bcbc04b86f197))
* make lesson acceptance finalization idempotent ([32298bc](https://github.com/e-kulikov/pisar/commit/32298bc9633389a913466d8a1da0fcfc573cae24))
* make lesson show recommend accept for a started acceptance ([0bedde7](https://github.com/e-kulikov/pisar/commit/0bedde748832d73a2879a549868b7a3e18dff035))
* make resumed and completed lesson acceptances independent of workspace files ([839808e](https://github.com/e-kulikov/pisar/commit/839808e9d122ea7b66a87546da2c39cfd4d72143))
* make the guard fail closed on malformed domain markers ([2578225](https://github.com/e-kulikov/pisar/commit/2578225b1fcfbf164001f003b6ca18a70098b518))
* measure the record size on the exact bytes that are stored ([413740f](https://github.com/e-kulikov/pisar/commit/413740fb1b95be03832a56c67efabad27d7dbe35))
* move spaces by blob identity so Git attributes never rewrite stored bytes ([40d21b4](https://github.com/e-kulikov/pisar/commit/40d21b49f651ab25b815f6a1484453e567e0bae7))
* name every locale variable passed to research claude instead of matching LC_ ([b097fb4](https://github.com/e-kulikov/pisar/commit/b097fb4f106d13376f610efa93317167964b9768))
* name the namespaced reviewer subagent in the lessons skill ([c3d01e8](https://github.com/e-kulikov/pisar/commit/c3d01e8bab847208f0a5834ef78501cd0d7a7f5a))
* never delete, move or repair plugin directories ([d670de8](https://github.com/e-kulikov/pisar/commit/d670de8a6e8311d16c26d7a4ac583113f696e01c))
* never let an unreadable draft fail a resumed lesson acceptance ([8c70739](https://github.com/e-kulikov/pisar/commit/8c7073933e389fa70befc4d04c803fcb8371af38))
* parse and validate the generated reviewer contract in tests ([bd7e838](https://github.com/e-kulikov/pisar/commit/bd7e838e810bff0ae94a47bc4acfc356e14eb3d5))
* pass research claude only an allowlist of environment variables ([d49ffe2](https://github.com/e-kulikov/pisar/commit/d49ffe2060b2f9c7b40d52fab70af303cce61ca0))
* pass research claude only vetted environment variable names ([c7e0ec1](https://github.com/e-kulikov/pisar/commit/c7e0ec19b6e8d6fd03ff436e6042c1f829883bcf))
* pin the parent gitlink to the child commit recorded by the acceptance ([1fdd05f](https://github.com/e-kulikov/pisar/commit/1fdd05f6b3816549abda73abfb084bdc98cd1e8b))
* publish the plugin as immutable content-hashed generations ([4c2fb68](https://github.com/e-kulikov/pisar/commit/4c2fb68c250592eba3ae91445efb8b4823f1406a))
* publish the plugin under a lock and recover interrupted replacements ([5e26aac](https://github.com/e-kulikov/pisar/commit/5e26aacea1b2380ce023a4cec4ef5fbb04e11a97))
* recognise every Git spelling of core.autocrlf true ([0855c3a](https://github.com/e-kulikov/pisar/commit/0855c3a336c9c454d417774697fbaa1504c3687b))
* record every recovered child commit, not only the first ([f41b385](https://github.com/e-kulikov/pisar/commit/f41b3859bc40c482a05e2652ec773fabdd59f697))
* recover only the operation-owned leftover of an interrupted atomic write ([635006e](https://github.com/e-kulikov/pisar/commit/635006e796b19b5af8160c57f4bcea28a56cca6e))
* refuse a lesson note whose path Git would not store byte for byte ([8096c13](https://github.com/e-kulikov/pisar/commit/8096c13602553695c4ebf0a849ad5afeabeb70dd))
* refuse new lesson revisions once acceptance has started ([5bb3e7a](https://github.com/e-kulikov/pisar/commit/5bb3e7acf2cc1c35215c048c4a10e5ee9c6c4eca))
* refuse symlinked or in-Git plugin and lesson workspace paths ([1a57340](https://github.com/e-kulikov/pisar/commit/1a573400950103d3c85b3efaec89ecc71c450b68))
* refuse to move a space that contains submodules ([6ea4677](https://github.com/e-kulikov/pisar/commit/6ea46778b78a8e8be234bce94a438bdc32295fee))
* refuse to resume a clone that has no recorded baseline ([db4f8c8](https://github.com/e-kulikov/pisar/commit/db4f8c8e26f54802db85567321f40f4a09b0ffc4))
* refuse unexpected directory symlinks when resuming a move ([0ca23ad](https://github.com/e-kulikov/pisar/commit/0ca23ad8ca23ec00404bb735f3d08198a891a26e))
* remove the module storage Git really uses when undoing a clone ([efdc099](https://github.com/e-kulikov/pisar/commit/efdc099735b753461a46a1efd8860e8f542db197))
* require finalized acceptance before discarding a lesson ([a974e4c](https://github.com/e-kulikov/pisar/commit/a974e4cc01e1562b9b3218a351c6ae92b7263c64))
* resume an interrupted lesson acceptance from its persisted revision ([134bd43](https://github.com/e-kulikov/pisar/commit/134bd43e8bece7807bade50ce4bade50cbe2e42b))
* resume an interrupted space operation only on its exact state ([a123c7c](https://github.com/e-kulikov/pisar/commit/a123c7c797d2754e8327089bf75d7bfa5ae3b1f6))
* rewrite any valid TOML string form in a moved marker ([58beb32](https://github.com/e-kulikov/pisar/commit/58beb328708f79ec4f6919e9f94ebbd3ca1c710d))
* run every shared-path Git call without hooks ([f8408b4](https://github.com/e-kulikov/pisar/commit/f8408b4d9d7635aed4e92b3d235a6fef250cf2b4))
* run the research claude in safe mode so shared hooks and plugins stay off ([156dde8](https://github.com/e-kulikov/pisar/commit/156dde85d2c5cf9fb0faa53aa3668aa06955b0ac))
* treat an edited uncommitted lesson note as a conflict, not a reset ([173b293](https://github.com/e-kulikov/pisar/commit/173b293a40523e828a0805c014fd2687031c9f12))
* validate a cloned domain marker before committing it ([e187f45](https://github.com/e-kulikov/pisar/commit/e187f4527fe0fb59e399e2b092a250ce31be4e03))
* validate references in the lesson body before accepting ([f6c08a0](https://github.com/e-kulikov/pisar/commit/f6c08a07aa618f64ef6d5cf1aebd6c868174eaa1))
* verify the child and .gitmodules when resuming a submodule add ([f83f6a3](https://github.com/e-kulikov/pisar/commit/f83f6a31559545ab1d2ac990f675eca15b5a9a06))
* write journaled blobs without Git filters and verify the committed bytes ([5adb8c7](https://github.com/e-kulikov/pisar/commit/5adb8c70c8bd1ce8bec08146485d51d4bbcef8a7))


### Documentation

* describe domain and space commands ([c29570e](https://github.com/e-kulikov/pisar/commit/c29570e6d5f842ee4922e9c41adcf96f9254a1e4))
* describe domains, addresses and --include/--exclude ([e6c342e](https://github.com/e-kulikov/pisar/commit/e6c342ef4c091f8d997fc45b805329ecfc442fc2))
* describe the config file, its precedence and `pisar config show` ([9cc01f6](https://github.com/e-kulikov/pisar/commit/9cc01f6d8aa4c95be79273eabe5a081e24c4f208))
* describe the lesson commands in the README and skill ([cd0b745](https://github.com/e-kulikov/pisar/commit/cd0b745c00fce382f2cc896350aa2c9c809f92aa))
* describe the plugin and launcher extensions ([b024f43](https://github.com/e-kulikov/pisar/commit/b024f430904c54742b7049b7be75561562a1cf34))
* document pisar research in the README and the agent skill ([fde9ed1](https://github.com/e-kulikov/pisar/commit/fde9ed1787dc7a7678c869038452f1df26c877e9))
* document the Agent and Skill tools of the launcher ([a8e7769](https://github.com/e-kulikov/pisar/commit/a8e7769c747519b07ba799527309517c5e521856))
* list --agent among the exceptions to the JSON output contract ([fde9600](https://github.com/e-kulikov/pisar/commit/fde9600626b0edbc08447aa1886ef53a12c3ce33))
* name the two network exceptions in the README and skill ([28a725e](https://github.com/e-kulikov/pisar/commit/28a725e22baa5d15cd2eed5ca89af396300d749a))
* **readme:** describe pisar guard check ([63ef160](https://github.com/e-kulikov/pisar/commit/63ef1605ed8341552b742ea6c024ec03480b23b5))
* say the outbound consent flag is needed only when the guard reports findings ([f169998](https://github.com/e-kulikov/pisar/commit/f16999862fee5ddf5ebce4ab26810b8a91bb7a3f))
* state the plain-text exceptions to the JSON output contract ([3292937](https://github.com/e-kulikov/pisar/commit/329293709c0a67ff9ec9a4a5d1e72257d1f78e2f))

## [0.3.0](https://github.com/e-kulikov/pisar/compare/v0.2.0...v0.3.0) (2026-10-07)


### Features

* add pisar --agent claude ([4425c3a](https://github.com/e-kulikov/pisar/commit/4425c3aa2d0a7a6fec6b07dae78e5149ab01dcfb))
* add pisar --agent claude ([a4e7f9a](https://github.com/e-kulikov/pisar/commit/a4e7f9a6da28a670a89ddfc3004fddaae693e8d4))
* load the root's .mcp.json and simplify the agent prompt ([922ab60](https://github.com/e-kulikov/pisar/commit/922ab608e559f25c582c97c150e8a379fa28b538))

## [0.2.0](https://github.com/e-kulikov/pisar/compare/v0.1.0...v0.2.0) (2026-10-06)


### Features

* add pisar --skill agent guide ([1d1a3c1](https://github.com/e-kulikov/pisar/commit/1d1a3c199257a5c5dd3f04074054d4dd4acff804))


### Documentation

* explain the name, document --skill and expand agent rules ([797bfcd](https://github.com/e-kulikov/pisar/commit/797bfcd8f002152c61092ed09772eaefd29c963b))

## 0.1.0 (2026-10-06)


### Features

* add the pisar CLI for Git-backed knowledge repositories ([aceffde](https://github.com/e-kulikov/pisar/commit/aceffdea93adf14bbc0a74e17033275b2f91b1f1))


### Bug Fixes

* **ruwana:** resolve a relative ruwana path before running in the root ([140df55](https://github.com/e-kulikov/pisar/commit/140df556cd566f97f2e1e3387f5fdb3e43e2f322))


### Documentation

* document mise strip_components and the pinned release commit ([f9f396a](https://github.com/e-kulikov/pisar/commit/f9f396a90a6c62e5da99dfb00b536da10e0111ec))
