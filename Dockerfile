# ベースイメージとして python:3.12-slim を選択 (Debianベース)
# slimイメージは、一般的な実行に必要なライブラリは含みつつ、
# ドキュメントや開発ツールなどを除いた軽量なイメージです。
FROM python:3.12-slim

# 環境変数の設定
# PYTHONDONTWRITEBYTECODE=1: Pythonが .pyc ファイルを生成しないようにします。
#                           コンテナ環境では不要な場合が多く、イメージサイズ削減にも繋がります。
# PYTHONUNBUFFERED=1: Pythonの標準出力・標準エラー出力をバッファリングせず、
#                     即座に出力するようにします。コンテナログの確認に役立ちます。
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# 作業ディレクトリを設定
# これ以降のRUN, COPY, CMDなどの命令はこのディレクトリを基準に実行されます。
WORKDIR /app

# requirements.txt を先にコピーします。
# このファイルに変更がない場合、後続の依存関係インストールステップの
# Dockerレイヤーキャッシュが利用され、ビルド時間を短縮できます。
COPY ./requirements.txt /app/requirements.txt

# システムのパッケージリストを更新し、Pythonパッケージのビルドに必要な依存関係をインストールします。
#   --no-install-recommends: 推奨パッケージのインストールをスキップし、最小限の依存関係のみを入れます。
#   --virtual .build-deps: これらの一時的なビルド用依存関係を仮想パッケージとしてグループ化し、
#                          後でまとめて削除しやすくします。
# 必要なツール:
#   gcc: Cコンパイラ
#   libffi-dev: Cで外部関数を呼び出すためのライブラリ (cryptographyなどで必要)
#   libssl-dev: OpenSSLライブラリの開発ヘッダー (cryptographyなどで必要)
#   cargo: Rustコンパイラ (cryptographyの一部のバックエンドはRustで書かれています)
#
# pip自体をアップグレードし、requirements.txt からPythonパッケージをインストールします。
#   --no-cache-dir: pipがキャッシュを使用しないようにし、イメージサイズを削減します。
#
# 最後に、ビルド用の一時的な依存関係とAPTキャッシュを削除して、イメージサイズを最適化します。
RUN apt-get update && \
    apt-get install -y --no-install-recommends --virtual .build-deps \
        gcc \
        libffi-dev \
        libssl-dev \
        cargo \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /app/requirements.txt \
    && apt-get purge -y --auto-remove .build-deps \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# アプリケーションコードを作業ディレクトリにコピーします。
# COPY ./app /app/app のように、ソースコードが 'app' サブディレクトリにある場合は
# それに合わせて調整してください。ここではカレントディレクトリの 'app' フォルダを想定しています。
COPY ./app /app/app

# アプリケーションがリッスンするポートを指定します。
# これはドキュメンテーションとしての意味合いが強く、実際にポートをホストにマッピングするのは
# `docker run` コマンドの `-p` オプションや、ECSのポートマッピング設定です。
EXPOSE 8000

# コンテナ起動時に実行されるデフォルトのコマンドを指定します。
# FastAPIアプリケーションをUvicornで起動します。
#   app.main_app:app : /app/app/main_app.py ファイル内の `app` というFastAPIインスタンスを指します。
#   --host 0.0.0.0  : コンテナ外部からのアクセスを許可します。
#   --port 8000     : EXPOSEで指定したポートでリッスンします。
CMD ["uvicorn", "app.main_app:app", "--host", "0.0.0.0", "--port", "8000"]
# ベースイメージを選択 (Python 3.9-slimなど、適切なバージョンを選択)
FROM python:alpine

# 作業ディレクトリを設定
WORKDIR /app

# 依存関係ファイルをコピーしてインストール
COPY ./requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

# アプリケーションコードをコピー
# 上記のディレクトリ構成例の場合、appフォルダをコンテナの/app/appにコピー
COPY ./app /app/app

# (オプション) 環境変数設定 (CDKのタスク定義でも設定可能)
# ENV VAR_NAME=value

# アプリケーションを実行するポートを公開
EXPOSE 8000

# アプリケーションの起動コマンド
# uvicornを直接実行。app.main_app:app は、/app/app/main_app.py の中の app インスタンスを指す
CMD ["uvicorn", "app.main_app:app", "--host", "0.0.0.0", "--port", "8000"]
