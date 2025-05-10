# main_app.py
from fastapi import FastAPI, Request, Depends, HTTPException, status
from typing import Optional, Dict
import logging
# cognito_utils.py から検証関数と設定値をインポート
from .cognito_utils import verify_cognito_token, COGNITO_USER_POOL_ID, COGNITO_REGION, COGNITO_APP_CLIENT_ID

app = FastAPI(
    title="FastAPI Backend Application (Plan B)",
    description="Cognitoで認証されたユーザー向けのバックエンドサービスです。JWTはアプリ自身で検証します。"
)

# ロガー設定 (アプリケーション全体で共通)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def get_current_user_claims(request: Request) -> Dict:
    """
    リクエストヘッダーからCognito JWTを取得し検証します。
    ALB1 (Cognito認証) は 'x-amzn-oidc-accesstoken' ヘッダーにアクセストークンを格納します。
    Nginxはこれをそのまま転送します。
    """
    token: Optional[str] = None
    # ALBが付与するアクセストークンヘッダー
    oidc_access_token = request.headers.get("x-amzn-oidc-accesstoken")
    # (オプション) Nginxが 'Authorization: Bearer <token>' 形式に変換している場合も考慮
    auth_header = request.headers.get("Authorization")

    if oidc_access_token:
        token = oidc_access_token
        logger.info("バックエンド: X-Amzn-Oidc-Accesstoken ヘッダーからトークンを取得。")
    elif auth_header:
        parts = auth_header.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]
            logger.info("バックエンド: Authorization ヘッダーからBearerトークンを取得。")
        else:
            logger.warning(f"バックエンド: 不正な形式のAuthorizationヘッダー: {auth_header}")
            # 401を返すか、トークン無しとして処理を続けるかは要件による
            # raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="不正な認証ヘッダー")
    
    if not token:
        logger.warning("バックエンド: 有効な認証トークンがヘッダーに見つかりません (x-amzn-oidc-accesstoken または Authorization)。")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="認証されていません。トークンが見つかりません。",
            headers={"WWW-Authenticate": "Bearer"}, # クライアントにBearerトークンが必要であることを示唆
        )

    try:
        logger.info(f"バックエンド: トークンを検証します (先頭20文字): {token[:20]}...")
        # アクセストークン ("access") を検証
        claims = verify_cognito_token(token, token_use="access")
        logger.info(f"バックエンド: トークンは正常に検証されました。ユーザー: {claims.get('username')}")
        return claims
    except ValueError as e: # verify_cognito_token が発生させるエラー
        logger.warning(f"バックエンド: トークンの検証に失敗しました: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"認証資格情報を検証できませんでした: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception as e: # 予期せぬエラー
        logger.error(f"バックエンド: トークン検証中に予期せぬエラー: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="認証処理中にサーバーエラーが発生しました。"
        )

@app.on_event("startup")
async def startup_event():
    logger.info("バックエンドアプリケーション (プランB) を開始します。")
    logger.info(f"Cognito設定: UserPoolID={COGNITO_USER_POOL_ID}, Region={COGNITO_REGION}, AppClientID={COGNITO_APP_CLIENT_ID}")
    if not all([COGNITO_USER_POOL_ID, COGNITO_REGION, COGNITO_APP_CLIENT_ID]):
        logger.error("Cognito関連の環境変数が一つ以上設定されていません。アプリケーションは正しく動作しない可能性があります。")
        # 起動を失敗させる場合はここで例外を発生させる
        # raise RuntimeError("Cognito設定が不完全です。")


@app.get("/")
async def read_root():
    return {"message": "FastAPIバックエンドアプリケーション (プランB) へようこそ！"}

@app.get("/users/me", summary="現在のユーザー情報を取得します")
async def read_users_me(current_user_claims: Dict = Depends(get_current_user_claims)):
    # current_user_claims にはCognito JWTの検証済みクレームが含まれる
    return {"user_info": current_user_claims}

@app.get("/protected-resource", summary="保護されたリソースにアクセスします")
async def get_protected_resource(current_user_claims: Dict = Depends(get_current_user_claims)):
    username = current_user_claims.get("username", "不明なユーザー")
    return {"message": f"ようこそ、{username}さん！これは保護されたリソースです。", "claims": current_user_claims}

@app.get("/debug/headers", summary="リクエストヘッダーをデバッグ表示します (開発用)")
async def debug_headers(request: Request):
    # 実際の運用ではこのエンドポイントは削除またはアクセス制限してください
    return {"headers": dict(request.headers)}

# uvicornで直接実行する場合 (ローカルテスト用)
if __name__ == "__main__":
    import uvicorn
    # 環境変数をローカルで設定する必要がある
    # 例:
    # export COGNITO_USER_POOL_ID="ap-northeast-1_xxxxxxxxx"
    # export COGNITO_REGION="ap-northeast-1"
    # export COGNITO_APP_CLIENT_ID="xxxxxxxxxxxxxxxxx"
    if not all([COGNITO_USER_POOL_ID, COGNITO_REGION, COGNITO_APP_CLIENT_ID]):
        print("警告: Cognito関連の環境変数が設定されていません。ローカル実行は失敗する可能性があります。")
        print("必要な環境変数: COGNITO_USER_POOL_ID, COGNITO_REGION, COGNITO_APP_CLIENT_ID")

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
# main_app.py
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer # 例として
from cognito_utils import verify_cognito_token # 検証ユーティリティを再利用可能
import logging

app = FastAPI()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# このバックエンドアプリが直接JWTを検証する場合の例
# ALB1 -> Nginx (パススルー) -> ALB2 -> このアプリ
# または ALB1 -> EC2(Nginx + FastAPI検証) -> ALB2 -> このアプリ (Nginxから検証済み情報を受け取る)

# OAuth2PasswordBearerはAuthorizationヘッダーのBearerトークンを期待する
# Cognitoからのトークンはx-amzn-oidc-accesstokenに入ってくるので、
# NginxでAuthorizationヘッダーにコピーするか、ここで直接x-amzn-oidc-accesstokenを読む
oauth2_scheme_cognito = OAuth2PasswordBearer(tokenUrl="cognito_login_url_placeholder", auto_error=False)

async def get_current_user_from_cognito(request: Request, token: str = Depends(oauth2_scheme_cognito)):
    cognito_access_token = request.headers.get("x-amzn-oidc-accesstoken")
    actual_token = token or cognito_access_token

    if not actual_token:
        logger.warning("No token found in Authorization header or x-amzn-oidc-accesstoken header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        logger.info(f"Backend: Verifying token from header: {actual_token[:20]}...")
        payload = verify_cognito_token(actual_token)
        username: str = payload.get("username")
        if username is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials (username missing)")
        return payload # ユーザーのクレーム全体を返す
    except Exception as e:
        logger.error(f"Backend: Token validation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Could not validate credentials: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )

@app.get("/")
async def read_root():
    return {"message": "Hello from FastAPI backend application!"}

@app.get("/protected-data")
async def read_protected_data(current_user: dict = Depends(get_current_user_from_cognito)):
    # current_user にはCognito JWTのクレームが含まれる
    return {"message": "This is protected data.", "user_info": current_user}

@app.get("/debug-headers")
async def debug_headers(request: Request):
    return {"headers": dict(request.headers)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)