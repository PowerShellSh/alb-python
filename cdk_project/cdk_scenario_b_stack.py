from aws_cdk import (
    Stack,
    RemovalPolicy,
    Duration,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_elasticloadbalancingv2_actions as elbv2_actions,
    aws_cognito as cognito,
    aws_iam as iam,
    aws_certificatemanager as acm,
    aws_autoscaling as autoscaling,
    aws_logs as logs,
    CfnOutput
)
from constructs import Construct
import os
import base64 # Base64エンコードのためにインポート

# --- 定数・設定値 ---
COGNITO_DOMAIN_PREFIX = os.getenv("COGNITO_DOMAIN_PREFIX", "my-unique-fastapi-app-auth-sample")
MY_DOMAIN_NAME = os.getenv("DOMAIN_NAME")
MY_HOSTED_ZONE_ID = os.getenv("HOSTED_ZONE_ID")
ALB1_CERTIFICATE_ARN = os.getenv("ALB1_CERTIFICATE_ARN")
NGINX_AMI_ID = os.getenv("NGINX_IMAGE_ID") # 指定がなければNone
FASTAPI_APP_IMAGE_URI = os.getenv("FASTAPI_APP_IMAGE_URI")
if not FASTAPI_APP_IMAGE_URI:
    raise ValueError("環境変数 FASTAPI_APP_IMAGE_URI が設定されていません。ECRのイメージURIを指定してください。")

class CdkScenarioBStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- 1. VPCの作成 ---
        vpc = ec2.Vpc(self, "MyVPC",
            max_azs=2,
            cidr="10.0.0.0/16",
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="PublicSubnet",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="PrivateSubnetWithEgress",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24
                ),
            ],
            nat_gateways=1
        )

        # --- 2. Cognitoユーザープールとクライアントの作成 ---
        user_pool = cognito.UserPool(self, "MyUserPool",
            user_pool_name=f"{construct_id}-user-pool",
            self_sign_up_enabled=True,
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            user_verification=cognito.UserVerificationConfig(
                email_style=cognito.VerificationEmailStyle.LINK
            ),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
                preferred_username=cognito.StandardAttribute(required=False, mutable=True)
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=False
            ),
            removal_policy=RemovalPolicy.DESTROY
        )

        user_pool_domain = user_pool.add_domain("CognitoDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=COGNITO_DOMAIN_PREFIX
            )
        )

        app_client_callback_urls = []
        app_client_logout_urls = []
        if MY_DOMAIN_NAME:
            app_client_callback_urls.append(f"https://{MY_DOMAIN_NAME}/oauth2/idpresponse")
            app_client_logout_urls.append(f"https://{MY_DOMAIN_NAME}/")
        else:
            app_client_callback_urls.append("https://placeholder.example.com/oauth2/idpresponse") # 仮のURL
            app_client_logout_urls.append("https://placeholder.example.com/") # 仮のURL


        user_pool_client = user_pool.add_client("AppClientAlb",
            user_pool_client_name=f"{construct_id}-alb-auth-client",
            auth_flows=cognito.AuthFlow(user_srp=True, user_password=True),
            generate_secret=False,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE
                ],
                callback_urls=app_client_callback_urls,
                logout_urls=app_client_logout_urls
            ),
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.COGNITO
            ],
            prevent_user_existence_errors=True
        )

        cognito_user_pool_id_for_app_actual = user_pool.user_pool_id
        cognito_region_for_app_actual = self.region
        cognito_app_client_id_for_app_actual = user_pool_client.user_pool_client_id

        # --- 3. ALB1 (フロント、インターネット向け) の作成 ---
        certificate_alb1 = None
        if MY_DOMAIN_NAME and MY_HOSTED_ZONE_ID:
            hosted_zone = elbv2.HostedZone.from_hosted_zone_attributes(self, "MyHostedZone",
                hosted_zone_id=MY_HOSTED_ZONE_ID,
                zone_name=MY_DOMAIN_NAME
            )
            certificate_alb1 = acm.Certificate(self, "Alb1Certificate",
                domain_name=MY_DOMAIN_NAME,
                validation=acm.CertificateValidation.from_dns(hosted_zone)
            )
        elif ALB1_CERTIFICATE_ARN:
             certificate_alb1 = acm.Certificate.from_certificate_arn(self, "Alb1CertImported", ALB1_CERTIFICATE_ARN)

        sg_alb1 = ec2.SecurityGroup(self, "SgAlb1", vpc=vpc, allow_all_outbound=True,
                                    description="Security group for ALB1 (front-facing)")
        sg_alb1.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "Allow HTTPS from anywhere")
        sg_alb1.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "Allow HTTP (for redirect)")

        alb1 = elbv2.ApplicationLoadBalancer(self, "Alb1",
            vpc=vpc,
            internet_facing=True,
            security_group=sg_alb1,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC)
        )
        alb1.add_redirect(source_protocol=elbv2.ApplicationProtocol.HTTP, source_port=80,
                          target_protocol=elbv2.ApplicationProtocol.HTTPS, target_port=443)

        # --- 4. Nginx EC2インスタンス (Auto Scaling Group) の作成 ---
        sg_nginx = ec2.SecurityGroup(self, "SgNginx", vpc=vpc, allow_all_outbound=True,
                                     description="Security group for Nginx EC2 instances")
        sg_nginx.add_ingress_rule(sg_alb1, ec2.Port.tcp(80), "Allow HTTP from ALB1")

        nginx_ec2_role = iam.Role(self, "NginxEc2Role",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMReadOnlyAccess")
            ]
        )

        # Nginx設定ファイルとセットアップスクリプトを読み込む
        current_dir = os.path.dirname(__file__)
        try:
            with open(os.path.join(current_dir, "nginx_config", "nginx.conf"), "r", encoding="utf-8") as f:
                nginx_conf_content = f.read()
            nginx_conf_content_base64 = base64.b64encode(nginx_conf_content.encode('utf-8')).decode('utf-8')

            with open(os.path.join(current_dir, "scripts", "nginx_setup.sh"), "r", encoding="utf-8") as f:
                nginx_setup_script_template = f.read()
        except FileNotFoundError as e:
            print(f"エラー: Nginx設定ファイルまたはセットアップスクリプトが見つかりません。パスを確認してください。詳細: {e}")
            raise

        alb2_dns_ssm_param_name = f"/{construct_id}/alb2/dnsName"

        nginx_user_data_script = nginx_setup_script_template.replace(
            "__ALB2_DNS_SSM_PARAM_NAME__", alb2_dns_ssm_param_name
        ).replace(
            "__REGION__", self.region
        ).replace(
            "__NGINX_CONFIG_CONTENT_BASE64__", nginx_conf_content_base64
        )

        nginx_user_data = ec2.UserData.for_linux()
        nginx_user_data.add_commands(nginx_user_data_script)

        if NGINX_AMI_ID:
            nginx_machine_image = ec2.MachineImage.generic_linux({self.region: NGINX_AMI_ID})
        else:
            nginx_machine_image = ec2.MachineImage.latest_amazon_linux(
                generation=ec2.AmazonLinuxGeneration.AMAZON_LINUX_2
            )

        nginx_asg = autoscaling.AutoScalingGroup(self, "NginxAsg",
            vpc=vpc,
            instance_type=ec2.InstanceType("t3.micro"),
            machine_image=nginx_machine_image,
            user_data=nginx_user_data,
            role=nginx_ec2_role,
            security_group=sg_nginx,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            min_capacity=1,
            max_capacity=2,
            desired_capacity=1,
            health_check=autoscaling.HealthCheck.ec2(grace=Duration.minutes(5))
        )

        nginx_target_group = elbv2.ApplicationTargetGroup(self, "NginxTg",
            vpc=vpc, port=80, protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.INSTANCE,
            targets=[nginx_asg],
            health_check=elbv2.HealthCheck(path="/", interval=Duration.seconds(60), timeout=Duration.seconds(5))
        )

        if certificate_alb1:
            https_listener = alb1.add_listener("HttpsListener", port=443, certificates=[certificate_alb1],
                ssl_policy=elbv2.SslPolicy.RECOMMENDED_TLS
            )
            https_listener.add_action("DefaultActionCognito",
                action=elbv2_actions.AuthenticateCognitoAction(
                    user_pool=user_pool,
                    user_pool_client=user_pool_client,
                    user_pool_domain=user_pool_domain,
                    next=elbv2.ListenerAction.forward([nginx_target_group]),
                    on_unauthenticated_request=elbv2.UnauthenticatedAction.AUTHENTICATE
                )
            )
        else:
            CfnOutput(self, "WarningNoCertificate",
                value="ALB1 HTTPS Listener not created due to missing certificate. Cognito authentication will not work.",
                description="WARNING: No SSL certificate provided for ALB1."
            )

        # --- 5. ALB2 (バックエンド、内部) の作成 ---
        sg_alb2 = ec2.SecurityGroup(self, "SgAlb2", vpc=vpc, allow_all_outbound=True,
                                    description="Security group for ALB2 (internal backend)")
        sg_alb2.add_ingress_rule(sg_nginx, ec2.Port.tcp(80), "Allow HTTP from Nginx ASG")

        alb2 = elbv2.ApplicationLoadBalancer(self, "Alb2",
            vpc=vpc,
            internet_facing=False,
            security_group=sg_alb2,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)
        )
        CfnOutput(self, "Alb2DnsNameSsmParameterHint",
            value=alb2_dns_ssm_param_name,
            description=f"Hint: Create SSM Parameter Store '{alb2_dns_ssm_param_name}' with value: {alb2.load_balancer_dns_name}"
        )
        CfnOutput(self, "Alb2ActualDnsName", value=alb2.load_balancer_dns_name, description="ALB2 Actual DNS Name")

        # --- 6. FastAPIバックエンド (ECS Fargateサービス) の作成 ---
        cluster = ecs.Cluster(self, "MyEcsCluster", vpc=vpc)

        fastapi_task_role = iam.Role(self, "FastApiTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com")
        )
        fastapi_execution_role = iam.Role(self, "FastApiExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonECSTaskExecutionRolePolicy")
            ]
        )

        log_group_fastapi = logs.LogGroup(self, "FastApiLogGroup",
            log_group_name=f"/ecs/{construct_id}/fastapi-app",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY
        )

        fastapi_task_definition = ecs.FargateTaskDefinition(self, "FastApiTaskDef",
            memory_limit_mib=512, cpu=256,
            task_role=fastapi_task_role,
            execution_role=fastapi_execution_role
        )

        fastapi_container = fastapi_task_definition.add_container("FastApiContainer",
            image=ecs.ContainerImage.from_registry(FASTAPI_APP_IMAGE_URI),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="fastapi", log_group=log_group_fastapi),
            port_mappings=[ecs.PortMapping(container_port=8000, host_port=8000, protocol=ecs.Protocol.TCP)],
            environment={
                "COGNITO_USER_POOL_ID": cognito_user_pool_id_for_app_actual,
                "COGNITO_REGION": cognito_region_for_app_actual,
                "COGNITO_APP_CLIENT_ID": cognito_app_client_id_for_app_actual,
                "FASTAPI_ENV": "production",
            }
        )

        sg_fastapi = ec2.SecurityGroup(self, "SgFastApi", vpc=vpc, allow_all_outbound=True,
                                       description="Security group for FastAPI Fargate service")
        sg_fastapi.add_ingress_rule(sg_alb2, ec2.Port.tcp(8000), "Allow HTTP from ALB2 on port 8000")

        fastapi_service = ecs.FargateService(self, "FastApiService",
            cluster=cluster,
            task_definition=fastapi_task_definition,
            desired_count=1,
            assign_public_ip=False,
            security_groups=[sg_fastapi],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
        )

        alb2_listener = alb2.add_listener("FastApiListener", port=80)
        alb2_listener.add_targets("FastApiTarget",
            port=80, # Listener port
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[fastapi_service.load_balancer_target(
                container_name="FastApiContainer",
                container_port=8000
            )],
            health_check=elbv2.HealthCheck(
                path="/",
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=2,
                port="8000"
            )
        )

        # --- 7. CloudFormation Outputs ---
        CfnOutput(self, "Alb1DnsOutput", value=alb1.load_balancer_dns_name, description="ALB1 DNS Name")
        CfnOutput(self, "CognitoUserPoolIdOutput", value=user_pool.user_pool_id, description="Cognito User Pool ID")
        CfnOutput(self, "CognitoAppClientIdOutput", value=user_pool_client.user_pool_client_id, description="Cognito App Client ID")
        CfnOutput(self, "CognitoDomainUrlOutput",
            value=f"https://{user_pool_domain.domain_name}.auth.{self.region}.amazoncognito.com",
            description="Cognito Hosted UI URL (for sign-in/sign-up)"
        )
        if MY_DOMAIN_NAME:
            CfnOutput(self, "ApplicationCustomDomainOutput", value=f"https://{MY_DOMAIN_NAME}/",
                description="Application Endpoint URL (if custom domain is configured)"
            )
        CfnOutput(self, "FastApiEcsLogGroupNameOutput", value=log_group_fastapi.log_group_name, description="CloudWatch Log Group name for FastAPI ECS service")