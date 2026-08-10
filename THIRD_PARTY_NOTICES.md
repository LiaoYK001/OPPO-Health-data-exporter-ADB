# Third-party notices

This project is licensed under the [MIT License](LICENSE). Its compiled Web assets redistribute the following components:

| Component | License | Copyright / notice |
| --- | --- | --- |
| Apache ECharts | Apache-2.0 | Copyright 2017–2026 The Apache Software Foundation |
| d3.js portions embedded by ECharts | BSD-3-Clause | Copyright 2010–2016 Mike Bostock |
| zrender | BSD-3-Clause | Copyright 2017 Baidu Inc. |
| React and React DOM | MIT | Copyright Meta Platforms, Inc. and affiliates |
| Lucide / Lucide React | ISC; Feather portions MIT | Copyright Cole Bemis and Lucide Contributors |

Apache ECharts includes software developed at the Apache Software Foundation. The complete attribution and license texts are distributed in [`third_party/`](third_party/):

- [Apache ECharts license](third_party/Apache-ECharts-LICENSE.txt)
- [Apache ECharts NOTICE](third_party/Apache-ECharts-NOTICE.txt)
- [Embedded d3.js license](third_party/d3-BSD-3-Clause.txt)
- [zrender license](third_party/zrender-BSD-3-Clause.txt)
- [React license](third_party/React-MIT.txt)
- [Lucide license](third_party/Lucide-ISC.txt)

Python runtime dependencies are installed separately rather than copied into this repository. At the time of this notice the direct dependencies use: defusedxml (PSF License), FastAPI (MIT), NumPy (BSD-3-Clause), openpyxl (MIT), Pillow (MIT-CMU), Uvicorn (BSD-3-Clause), and optional Frida (wxWindows Library Licence 3.1). Their installed distributions contain the authoritative license texts. Dependency versions and licenses must be rechecked when updating lock files or publishing a binary bundle.

This notice is informational and does not change any third-party license.
