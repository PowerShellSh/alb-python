# cognito_utils.py
import os
import requests
from jose import jwt, jwk
from jose.utils import base64url_decode
from cachetools import cached, TTLCache
import logging

# ロガー設定
logger = logging.getLogger(__name__)
# logging.basicConfig(level=logging.INFO) # main_app.pyで設定するためここではコメントアウトも可

# Cognitoの設定 (環境変数から取得することを強く推奨)
# CDKでECSタスク定義の環境変数として設定される想定
COGNITO_USER_POOL_ID = os.getenv("COGNITO_USER_POOL_ID")
COGNITO_REGION = os.getenv("COGNITO_REGION")
COGNITO_APP_CLIENT_ID = os.getenv("COGNITO_APP_CLIENT_ID") # 検証対象のApp Client ID (audience)

# JWKS (JSON Web Key Set) のURL
# 環境変数が設定されていない場合のエラーを避けるため、初期化は関数内で行う
JWKS_URL = None
if COGNITO_REGION and COGNITO_USER_POOL_ID:
    JWKS_URL = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}/.well-known/jwks.json"

# 公開鍵をキャッシュ (例: 1時間TTL)
# TTLCache(maxsize=キャッシュするアイテムの最大数, ttl=キャッシュの有効期間(秒))
jwks_cache = TTLCache(maxsize=1, ttl=3600)

@cached(jwks_cache) # 結果をキャッシュする
def get_cognito_public_keys():
    """
    Cognitoユーザープールの公開鍵セット(JWKS)を取得します。
    取得した公開鍵はキャッシュされます。
    """
    if not JWKS_URL:
        logger.error("Cognito設定 (USER_POOL_ID, REGION) が不完全なため、JWKS URLを構築できません。環境変数を確認してください。")
        raise ValueError("Cognito設定が不足しています。JWKS URLを構築できません。")
    try:
        logger.info(f"{JWKS_URL} から公開鍵を取得します。")
        response = requests.get(JWKS_URL, timeout=5) # タイムアウトを設定
        response.raise_for_status()  # HTTPエラーがあれば例外を発生
        return response.json()["keys"]
    except requests.exceptions.RequestException as e:
        logger.error(f"JWKSの取得に失敗しました: {e}")
        raise ValueError(f"JWKSの取得に失敗しました: {e}")


def verify_cognito_token(token: str, token_use: str = "access"):
    """
    Cognitoから発行されたJWTを検証します。

    Args:
        token (str): 検証するJWT文字列。
        token_use (str): トークンの種類 ("access" または "id")。デフォルトは "access"。

    Returns:
        dict: 検証済みのJWTクレーム。

    Raises:
        ValueError: トークンの検証に失敗した場合。
    """
    if not token:
        logger.warning("検証対象のトークンが空です。")
        raise ValueError("トークンが空です。")
    if not COGNITO_APP_CLIENT_ID:
        logger.error("環境変数 COGNITO_APP_CLIENT_ID が設定されていません。トークンのaudienceを検証できません。")
        raise ValueError("COGNITO_APP_CLIENT_ID が設定されていません。")
    if not JWKS_URL: # JWKS_URLがNoneの場合（初期設定不足）
        logger.error("Cognito設定が不足しているため、トークンを検証できません。")
        raise ValueError("Cognito設定が不足しています。")


    public_keys = get_cognito_public_keys() # JWKS_URLがNoneならここでエラー
    try:
        headers = jwt.get_unverified_headers(token)
    except jwt.JWTError as e:
        logger.warning(f"トークンヘッダーの解析に失敗しました: {e}")
        raise ValueError(f"無効なトークンヘッダーです: {e}")

    kid = headers.get("kid")
    if not kid:
        logger.warning("トークンヘッダーに 'kid' が含まれていません。")
        raise ValueError("トークンヘッダーに 'kid' が含まれていません。")

    # kidに一致する公開鍵を探す
    key_data = next((key for key in public_keys if key["kid"] == kid), None)
    if not key_data:
        logger.warning(f"'kid' ({kid}) に一致する公開鍵がJWKSに見つかりませんでした。キャッシュをクリアして再試行する可能性があります。")
        # キャッシュをクリアして再取得を試みる (オプション)
        # jwks_cache.clear()
        # public_keys = get_cognito_public_keys()
        # key_data = next((key for key in public_keys if key["kid"] == kid), None)
        # if not key_data:
        raise ValueError(f"'kid' ({kid}) に一致する公開鍵がJWKSに見つかりません。")

    try:
        # 公開鍵をJWK形式で構築
        public_key_jwk = jwk.construct(key_data)
        # 公開鍵をPEM形式の文字列に変換 (python-joseのdecodeメソッドのkey引数に渡すため)
        public_key_pem = public_key_jwk.to_pem().decode('utf-8')

        # トークンをデコードおよび検証
        # algorithms=["RS256"] はCognitoのデフォルト
        # audience は検証したいApp Client ID
        # issuer はCognitoユーザープールのURL
        # options で leeway を設定すると、クロックスキュー（時刻のズレ）を許容できる
        verified_claims = jwt.decode(
            token,
            public_key_pem,
            algorithms=["RS256"],
            audience=COGNITO_APP_CLIENT_ID,
            issuer=f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}",
            options={"verify_exp": True, "verify_nbf": True, "verify_iat": True, "leeway": 60} # 60秒の時刻ズレを許容
        )

        # token_useクレームの検証
        if verified_claims.get("token_use") != token_use:
            logger.warning(f"トークンの'token_use' ({verified_claims.get('token_use')}) が期待値 ({token_use}) と異なります。")
            raise ValueError(f"トークンの'token_use' ({verified_claims.get('token_use')}) が期待値 ({token_use}) と異なります。")

        logger.info(f"トークンは正常に検証されました。ユーザー: {verified_claims.get('username')}, クライアントID: {verified_claims.get('client_id')}")
        return verified_claims

    except jwt.ExpiredSignatureError:
        logger.warning("トークンの有効期限が切れています。")
        raise ValueError("トークンの有効期限が切れています。")
    except jwt.JWTClaimsError as e:
        logger.warning(f"トークンのクレームが無効です: {e}")
        raise ValueError(f"トークンのクレームが無効です: {e}")
    except jwt.JWTError as e: # その他のJWT関連エラー
        logger.warning(f"トークンの検証中にJWTエラーが発生しました: {e}")
        raise ValueError(f"無効なトークンです: {e}")
    except Exception as e:
        logger.error(f"トークン検証中に予期せぬエラーが発生しました: {e}", exc_info=True) # スタックトレースも記録
        raise ValueError(f"トークン検証中にエラーが発生しました: {e}")

# モジュールロード時に環境変数が設定されているか確認し、警告を出す
if not all([COGNITO_USER_POOL_ID, COGNITO_REGION, COGNITO_APP_CLIENT_ID]):
    logger.warning(
        "Cognito関連の環境変数 (COGNITO_USER_POOL_ID, COGNITO_REGION, COGNITO_APP_CLIENT_ID) "
        "が一つ以上設定されていません。verify_cognito_token関数は環境変数が全て設定されるまで正しく動作しません。"
    )
elif not JWKS_URL: # これは上記条件が満たされていても発生しうる（ロジック上の念のためチェック）
     logger.warning("JWKS_URLが初期化できませんでした。Cognito設定を確認してください。")
# cognito_utils.py
import os
import requests
from jose import jwt, jwk
from jose.utils import base64url_decode
from cachetools import cached, TTLCache
import logging

# ロガー設定
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Cognitoの設定 (環境変数から取得することを推奨)
# 例: export COGNITO_USER_POOL_ID="ap-northeast-1_xxxxxxxx"
#     export COGNITO_REGION="ap-northeast-1"
#     export COGNITO_APP_CLIENT_ID="xxxxxxxxxxxxxxxxx"
COGNITO_USER_POOL_ID = os.getenv("COGNITO_USER_POOL_ID")
COGNITO_REGION = os.getenv("COGNITO_REGION")
COGNITO_APP_CLIENT_ID = os.getenv("COGNITO_APP_CLIENT_ID") # 検証対象のApp Client ID (audience)

# JWKS (JSON Web Key Set) のURL
JWKS_URL = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}/.well-known/jwks.json"

# 公開鍵をキャッシュ (例: 1時間TTL)
# TTLCache(maxsize=キャッシュするアイテムの最大数, ttl=キャッシュの有効期間(秒))
jwks_cache = TTLCache(maxsize=1, ttl=3600)

@cached(jwks_cache) # 結果をキャッシュする
def get_cognito_public_keys():
    """
    Cognitoユーザープールの公開鍵セット(JWKS)を取得します。
    取得した公開鍵はキャッシュされます。
    """
    if not all([COGNITO_USER_POOL_ID, COGNITO_REGION]):
        logger.error("Cognito設定 (USER_POOL_ID, REGION) がされていません。環境変数を確認してください。")
        raise ValueError("Cognito設定が不足しています。")
    try:
        logger.info(f"{JWKS_URL} から公開鍵を取得します。")
        response = requests.get(JWKS_URL, timeout=5) # タイムアウトを設定
        response.raise_for_status()  # HTTPエラーがあれば例外を発生
        return response.json()["keys"]
    except requests.exceptions.RequestException as e:
        logger.error(f"JWKSの取得に失敗しました: {e}")
        raise ValueError(f"JWKSの取得に失敗しました: {e}")


def verify_cognito_token(token: str, token_use: str = "access"):
    """
    Cognitoから発行されたJWTを検証します。

    Args:
        token (str): 検証するJWT文字列。
        token_use (str): トークンの種類 ("access" または "id")。デフォルトは "access"。

    Returns:
        dict: 検証済みのJWTクレーム。

    Raises:
        ValueError: トークンの検証に失敗した場合。
    """
    if not token:
        raise ValueError("トークンが空です。")

    public_keys = get_cognito_public_keys()
    try:
        headers = jwt.get_unverified_headers(token)
    except jwt.JWTError as e:
        logger.warning(f"トークンヘッダーの解析に失敗しました: {e}")
        raise ValueError(f"無効なトークンヘッダーです: {e}")

    kid = headers.get("kid")
    if not kid:
        raise ValueError("トークンヘッダーに 'kid' が含まれていません。")

    # kidに一致する公開鍵を探す
    key_data = next((key for key in public_keys if key["kid"] == kid), None)
    if not key_data:
        logger.warning(f"'kid' ({kid}) に一致する公開鍵がJWKSに見つかりませんでした。キャッシュをクリアして再試行する可能性があります。")
        # キャッシュをクリアして再取得を試みる (オプション)
        # jwks_cache.clear()
        # public_keys = get_cognito_public_keys()
        # key_data = next((key for key in public_keys if key["kid"] == kid), None)
        # if not key_data:
        raise ValueError(f"'kid' ({kid}) に一致する公開鍵がJWKSに見つかりません。")

    try:
        # 公開鍵をJWK形式で構築
        public_key_jwk = jwk.construct(key_data)
        # 公開鍵をPEM形式の文字列に変換 (python-joseのdecodeメソッドのkey引数に渡すため)
        public_key_pem = public_key_jwk.to_pem().decode('utf-8')

        # トークンをデコードおよび検証
        # algorithms=["RS256"] はCognitoのデフォルト
        # audience は検証したいApp Client ID
        # issuer はCognitoユーザープールのURL
        # options で leeway を設定すると、クロックスキュー（時刻のズレ）を許容できる
        verified_claims = jwt.decode(
            token,
            public_key_pem,
            algorithms=["RS256"],
            audience=COGNITO_APP_CLIENT_ID,
            issuer=f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}",
            options={"verify_exp": True, "verify_nbf": True, "verify_iat": True, "leeway": 60} # 60秒の時刻ズレを許容
        )

        # token_useクレームの検証
        if verified_claims.get("token_use") != token_use:
            raise ValueError(f"トークンの'token_use' ({verified_claims.get('token_use')}) が期待値 ({token_use}) と異なります。")

        logger.info(f"トークンは正常に検証されました。ユーザー: {verified_claims.get('username')}, クライアントID: {verified_claims.get('client_id')}")
        return verified_claims

    except jwt.ExpiredSignatureError:
        logger.warning("トークンの有効期限が切れています。")
        raise ValueError("トークンの有効期限が切れています。")
    except jwt.JWTClaimsError as e:
        logger.warning(f"トークンのクレームが無効です: {e}")
        raise ValueError(f"トークンのクレームが無効です: {e}")
    except jwt.JWTError as e: # その他のJWT関連エラー
        logger.warning(f"トークンの検証中にJWTエラーが発生しました: {e}")
        raise ValueError(f"無効なトークンです: {e}")
    except Exception as e:
        logger.error(f"トークン検証中に予期せぬエラーが発生しました: {e}")
        raise ValueError(f"トークン検証中にエラーが発生しました: {e}")

# 環境変数が設定されているか確認 (モジュールロード時)
if not all([COGNITO_USER_POOL_ID, COGNITO_REGION, COGNITO_APP_CLIENT_ID]):
    logger.warning(
        "Cognito関連の環境変数 (COGNITO_USER_POOL_ID, COGNITO_REGION, COGNITO_APP_CLIENT_ID) "
        "が一つ以上設定されていません。verify_cognito_token関数は正しく動作しない可能性があります。"
    )