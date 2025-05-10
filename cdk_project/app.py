#!/usr/bin/env python3
import os
import aws_cdk as cdk

# スタック定義ファイルをインポート
# from cdk_project.cdk_scenario_b_stack import CdkScenarioBStack
# 上記のパスはプロジェクトの構成によって調整してください。
# もし app.py と cdk_scenario_b_stack.py が同じディレクトリにあれば以下のようにします。
from cdk_scenario_b_stack import CdkScenarioBStack # スタック定義ファイル名に合わせてください

app = cdk.App()

# スタック名を指定してインスタンス化
# 環境変数からAWSアカウントIDとリージョンを取得することを推奨
# CDK_DEFAULT_ACCOUNT と CDK_DEFAULT_REGION はCDKがデフォルトで使用する環境変数
aws_env = cdk.Environment(
    account=os.getenv('CDK_DEFAULT_ACCOUNT'),
    region=os.getenv('CDK_DEFAULT_REGION')
)

# スタックのインスタンス化。スタック名はCloudFormation上で表示される名前になります。
# 例: "MyFastApiNginxAlbStack" など、プロジェクトに合わせた名前に変更可能です。
CdkScenarioBStack(app, "CdkScenarioBStack", # ここで使用する名前がCloudFormationスタック名になります
    env=aws_env
    # 他にスタックに渡したいプロパティがあればここで指定
    #例: description="My FastAPI application stack with Nginx and ALB (Plan B)"
    )

app.synth()