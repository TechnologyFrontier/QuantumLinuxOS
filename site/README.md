# 量子OS リリースページ

量子OS v0.1 の紹介ページ（静的 HTML 1 枚と画面写真）です。ビルドは不要で、`site/` をそのまま静的ホスティング（GitHub Pages など）に置けば表示できます。
フォントは Google Fonts から読み込みます。画面写真は [docs/evaluation/desktop-0.1](../docs/evaluation/desktop-0.1/README.md) の評価で撮影したものです。

掲載している数値・仕様は v0.1 のソースコードと評価記録に基づきます。版を上げたときは、ヒーローの数値、エディションの表、動作環境の表を見直してください。

## 公開

- **GitHub Pages**: `main` の `site/` が変わると [.github/workflows/pages.yml](../.github/workflows/pages.yml) が公開します。
  最初に一度だけ、リポジトリの Settings → Pages → Build and deployment の Source を「GitHub Actions」にしてください。
  公開先は `https://technologyfrontier.github.io/QuantumLinuxOS/` です。
- **自社サイト**: 掲載用の一式を次からダウンロードできます（GitHub Pages の公開と同時に `site/` から自動で作り直されます）。
  - https://technologyfrontier.github.io/QuantumLinuxOS/quantumos-release-site.zip
  - https://technologyfrontier.github.io/QuantumLinuxOS/quantumos-release-site.zip.sha256（SHA-256）

  zip には `quantumos/index.html` と `quantumos/assets/` が入っています。パスはすべて相対なので、`quantumos/` を任意の場所に置けば表示できます。
