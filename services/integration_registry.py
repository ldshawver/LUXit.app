"""
Integration Service Registry
Defines metadata for all supported integrations, including the exact key
names used when storing secrets in the CompanySecret table (stored_key).
"""


class IntegrationServiceRegistry:
    """Registry of all supported integration services"""

    SERVICES = {
        # ── AI & Content ─────────────────────────────────────────────────────
        'openai': {
            'slug': 'openai',
            'display_name': 'OpenAI / GPT-4o',
            'category': 'AI & Content',
            'icon': '🤖',
            'description': 'AI-powered content generation, chatbot, and campaign automation',
            'color': '#10a37f',
            'config_fields': {},
            'secret_fields': {
                'api_key': {
                    'label': 'API Key',
                    'type': 'password',
                    'stored_key': 'OPENAI_API_KEY',
                    'placeholder': 'sk-proj-...',
                    'required': True,
                    'help_text': 'Get this from platform.openai.com → API keys',
                }
            },
        },

        # ── Social Media ─────────────────────────────────────────────────────
        'twitter': {
            'slug': 'twitter',
            'display_name': 'X (Twitter)',
            'category': 'Social Media',
            'icon': '𝕏',
            'description': 'Post tweets, schedule content, and track engagement',
            'color': '#000000',
            'config_fields': {},
            'secret_fields': {
                'api_key': {
                    'label': 'Consumer Key (API Key)',
                    'type': 'password',
                    'stored_key': 'TWITTER_API_KEY',
                    'placeholder': 'aBcDeFgH12345...',
                    'required': True,
                    'help_text': 'developer.x.com → Apps → Keys and tokens → Consumer key',
                },
                'api_secret': {
                    'label': 'Consumer Secret (API Key Secret)',
                    'type': 'password',
                    'stored_key': 'TWITTER_API_SECRET',
                    'placeholder': 'xYzAbC98765...',
                    'required': True,
                    'help_text': 'developer.x.com → Apps → Keys and tokens → Consumer secret',
                },
                'bearer_token': {
                    'label': 'Bearer Token',
                    'type': 'password',
                    'stored_key': 'TWITTER_BEARER_TOKEN',
                    'placeholder': 'AAAA...',
                    'required': False,
                    'help_text': 'Used for read-only API calls',
                },
                'client_id': {
                    'label': 'OAuth 2.0 Client ID',
                    'type': 'text',
                    'stored_key': 'TWITTER_CLIENT_ID',
                    'placeholder': 'abc123XYZ...',
                    'required': False,
                    'help_text': 'Required for OAuth login flow',
                },
                'client_secret': {
                    'label': 'OAuth 2.0 Client Secret',
                    'type': 'password',
                    'stored_key': 'TWITTER_CLIENT_SECRET',
                    'placeholder': 'xyz789...',
                    'required': False,
                },
            },
        },

        'instagram': {
            'slug': 'instagram',
            'display_name': 'Instagram',
            'category': 'Social Media',
            'icon': '📸',
            'description': 'Publish posts, reels, and stories to Instagram Business',
            'color': '#e4405f',
            'config_fields': {
                'business_account_id': {
                    'label': 'Business Account ID',
                    'type': 'text',
                    'stored_key': 'INSTAGRAM_BUSINESS_ACCOUNT_ID',
                    'placeholder': '123456789',
                    'required': False,
                    'help_text': 'Your Instagram Business Account ID',
                },
            },
            'secret_fields': {
                'access_token': {
                    'label': 'Access Token',
                    'type': 'password',
                    'stored_key': 'INSTAGRAM_ACCESS_TOKEN',
                    'placeholder': 'EAAx...',
                    'required': True,
                    'help_text': 'Long-lived page access token from Meta Developer Console',
                },
                'user_token': {
                    'label': 'User Token',
                    'type': 'password',
                    'stored_key': 'INSTAGRAM_USER_TOKEN',
                    'placeholder': 'EAAx...',
                    'required': False,
                    'help_text': 'User-level token (optional, for user-specific actions)',
                },
            },
        },

        'facebook': {
            'slug': 'facebook',
            'display_name': 'Facebook',
            'category': 'Social Media',
            'icon': '👍',
            'description': 'Manage Facebook Pages, publish posts, and run ads',
            'color': '#1877f2',
            'config_fields': {
                'app_id': {
                    'label': 'App ID',
                    'type': 'text',
                    'stored_key': 'facebook_app_id',
                    'placeholder': '1234567890',
                    'required': True,
                    'help_text': 'developers.facebook.com → Your App → Settings → Basic',
                },
                'page_id': {
                    'label': 'Page ID',
                    'type': 'text',
                    'stored_key': 'FACEBOOK_PAGE_ID',
                    'placeholder': '1234567890',
                    'required': False,
                    'help_text': 'Your Facebook Page ID',
                },
                'webhook_verify_token': {
                    'label': 'Webhook Verify Token',
                    'type': 'text',
                    'stored_key': 'fb_webhook_verify_token',
                    'placeholder': 'your_custom_token',
                    'required': False,
                    'help_text': 'Any string you choose — must match what you set in Meta webhooks',
                },
            },
            'secret_fields': {
                'app_secret': {
                    'label': 'App Secret',
                    'type': 'password',
                    'stored_key': 'facebook_app_secret',
                    'placeholder': 'abc123...',
                    'required': True,
                    'help_text': 'developers.facebook.com → App → Settings → Basic → App Secret',
                },
                'access_token': {
                    'label': 'Page Access Token',
                    'type': 'password',
                    'stored_key': 'FACEBOOK_ACCESS_TOKEN',
                    'placeholder': 'EAAx...',
                    'required': False,
                    'help_text': 'Long-lived page access token',
                },
                'app_token': {
                    'label': 'App Token',
                    'type': 'password',
                    'stored_key': 'FACEBOOK_APP_TOKEN',
                    'placeholder': 'app_id|app_secret',
                    'required': False,
                    'help_text': 'App-level access token for webhooks',
                },
            },
        },

        'tiktok': {
            'slug': 'tiktok',
            'display_name': 'TikTok',
            'category': 'Social Media',
            'icon': '🎵',
            'description': 'Publish videos and manage TikTok for Business account',
            'color': '#ff0050',
            'config_fields': {
                'redirect_uri': {
                    'label': 'Redirect URI', 'type': 'url', 'stored_key': 'TIKTOK_REDIRECT_URI',
                    'placeholder': 'https://app.example.com/api/oauth/tiktok/callback', 'required': True,
                    'help_text': 'Use HTTPS for web mode, or loopback http://127.0.0.1:PORT/callback/ for desktop mode.',
                },
                'oauth_mode': {
                    'label': 'OAuth Mode: web/desktop', 'type': 'text', 'stored_key': 'TIKTOK_OAUTH_MODE',
                    'placeholder': 'web', 'required': True, 'help_text': 'Allowed values: web or desktop.',
                },
                'scopes': {
                    'label': 'Scopes', 'type': 'textarea', 'stored_key': 'TIKTOK_SCOPES',
                    'placeholder': 'user.info.basic video.publish', 'required': True,
                    'help_text': 'Space or comma-separated TikTok scopes.',
                },
                'allowed_media_domains': {
                    'label': 'Allowed/verified media domains', 'type': 'textarea', 'stored_key': 'TIKTOK_ALLOWED_MEDIA_DOMAINS',
                    'placeholder': 'cdn.example.com, media.example.com', 'required': False,
                    'help_text': 'Comma-separated domains permitted for pull-from-url posting.',
                },
                'enable_login_kit': {
                    'label': 'Enable Login Kit', 'type': 'text', 'stored_key': 'TIKTOK_ENABLE_LOGIN_KIT',
                    'placeholder': 'true', 'required': False,
                },
                'enable_content_posting_api': {
                    'label': 'Enable Content Posting API', 'type': 'text', 'stored_key': 'TIKTOK_ENABLE_CONTENT_POSTING_API',
                    'placeholder': 'true', 'required': False,
                },
                'enable_direct_post': {
                    'label': 'Enable Direct Post', 'type': 'text', 'stored_key': 'TIKTOK_ENABLE_DIRECT_POST',
                    'placeholder': 'true', 'required': False,
                },
            },
            'secret_fields': {
                'client_key': {
                    'label': 'Client Key',
                    'type': 'password',
                    'stored_key': 'TIKTOK_CLIENT_KEY',
                    'placeholder': 'awxxxxxxxxx',
                    'required': True,
                    'help_text': 'developers.tiktok.com → App → App Key',
                },
                'client_secret': {
                    'label': 'Client Secret (OAuth)',
                    'type': 'password',
                    'stored_key': 'TIKTOK_CLIENT_SECRET',
                    'placeholder': 'client_secret...',
                    'required': True,
                    'help_text': 'developers.tiktok.com → App → App Secret',
                },
            },
        },

        'linkedin': {
            'slug': 'linkedin',
            'display_name': 'LinkedIn',
            'category': 'Social Media',
            'icon': '💼',
            'description': 'Publish posts and manage LinkedIn Company Pages',
            'color': '#0a66c2',
            'config_fields': {},
            'secret_fields': {
                'client_id': {
                    'label': 'Client ID',
                    'type': 'text',
                    'stored_key': 'LINKEDIN_CLIENT_ID',
                    'placeholder': 'client_id...',
                    'required': True,
                    'help_text': 'developer.linkedin.com → App → Auth → Client ID',
                },
                'client_secret': {
                    'label': 'Client Secret',
                    'type': 'password',
                    'stored_key': 'LINKEDIN_CLIENT_SECRET',
                    'placeholder': 'client_secret...',
                    'required': True,
                },
                'access_token': {
                    'label': 'Access Token',
                    'type': 'password',
                    'stored_key': 'LINKEDIN_ACCESS_TOKEN',
                    'placeholder': 'access_token...',
                    'required': False,
                    'help_text': 'Long-lived token; auto-refreshed via OAuth',
                },
            },
        },

        'youtube': {
            'slug': 'youtube',
            'display_name': 'YouTube',
            'category': 'Social Media',
            'icon': '🎬',
            'description': 'Upload videos and manage your YouTube Channel',
            'color': '#ff0000',
            'config_fields': {
                'channel_id': {
                    'label': 'Channel ID',
                    'type': 'text',
                    'stored_key': 'YOUTUBE_CHANNEL_ID',
                    'placeholder': 'UCxxxxxxxxxx',
                    'required': False,
                    'help_text': 'Your YouTube Channel ID',
                },
            },
            'secret_fields': {
                'api_key': {
                    'label': 'API Key',
                    'type': 'password',
                    'stored_key': 'YOUTUBE_API_KEY',
                    'placeholder': 'AIza...',
                    'required': True,
                    'help_text': 'console.cloud.google.com → APIs → YouTube Data API v3',
                },
            },
        },

        'reddit': {
            'slug': 'reddit',
            'display_name': 'Reddit',
            'category': 'Social Media',
            'icon': '🤖',
            'description': 'Post to subreddits and engage with Reddit communities',
            'color': '#ff4500',
            'config_fields': {
                'username': {
                    'label': 'Reddit Username',
                    'type': 'text',
                    'stored_key': 'REDDIT_USERNAME',
                    'placeholder': 'u/yourname',
                    'required': True,
                },
            },
            'secret_fields': {
                'client_id': {
                    'label': 'Client ID',
                    'type': 'text',
                    'stored_key': 'REDDIT_CLIENT_ID',
                    'placeholder': 'client_id...',
                    'required': True,
                    'help_text': 'reddit.com/prefs/apps → your app → Client ID',
                },
                'client_secret': {
                    'label': 'Client Secret',
                    'type': 'password',
                    'stored_key': 'REDDIT_CLIENT_SECRET',
                    'placeholder': 'client_secret...',
                    'required': True,
                },
                'password': {
                    'label': 'Reddit Password',
                    'type': 'password',
                    'stored_key': 'REDDIT_PASSWORD',
                    'placeholder': 'your reddit password',
                    'required': True,
                    'help_text': 'Used for script-type OAuth authentication',
                },
            },
        },

        'snapchat': {
            'slug': 'snapchat',
            'display_name': 'Snapchat',
            'category': 'Social Media',
            'icon': '👻',
            'description': 'Publish Snaps and manage Snapchat Business account',
            'color': '#fffc00',
            'config_fields': {
                'business_account_id': {
                    'label': 'Business Account ID',
                    'type': 'text',
                    'stored_key': 'SNAPCHAT_BUSINESS_ACCOUNT_ID',
                    'placeholder': 'account_id...',
                    'required': False,
                },
            },
            'secret_fields': {
                'access_token': {
                    'label': 'Access Token',
                    'type': 'password',
                    'stored_key': 'SNAPCHAT_ACCESS_TOKEN',
                    'placeholder': 'access_token...',
                    'required': True,
                    'help_text': 'businesshelp.snapchat.com → Marketing API',
                },
            },
        },

        # ── Email & Calendar ─────────────────────────────────────────────────
        'ms365': {
            'slug': 'ms365',
            'display_name': 'Microsoft 365 / Outlook',
            'category': 'Email & Calendar',
            'icon': '☁️',
            'description': 'Send emails and manage calendar via Microsoft Graph API',
            'color': '#0078d4',
            'config_fields': {
                'tenant_id': {
                    'label': 'Tenant ID',
                    'type': 'text',
                    'stored_key': 'MS365_TENANT_ID',
                    'placeholder': 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx',
                    'required': True,
                    'help_text': 'portal.azure.com → Azure Active Directory → Overview',
                },
            },
            'secret_fields': {
                'client_id': {
                    'label': 'Client ID (App ID)',
                    'type': 'text',
                    'stored_key': 'MS365_CLIENT_ID',
                    'placeholder': 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx',
                    'required': True,
                    'help_text': 'portal.azure.com → App registrations → your app',
                },
                'client_secret': {
                    'label': 'Client Secret',
                    'type': 'password',
                    'stored_key': 'MS365_CLIENT_SECRET',
                    'placeholder': 'client_secret...',
                    'required': True,
                },
            },
        },

        'smtp': {
            'slug': 'smtp',
            'display_name': 'SMTP Email',
            'category': 'Email & Calendar',
            'icon': '📧',
            'description': 'Send transactional and campaign emails via any SMTP server',
            'color': '#6366f1',
            'config_fields': {
                'host': {
                    'label': 'SMTP Host',
                    'type': 'text',
                    'stored_key': 'SMTP_HOST',
                    'placeholder': 'smtp.gmail.com',
                    'required': True,
                },
                'port': {
                    'label': 'SMTP Port',
                    'type': 'number',
                    'stored_key': 'SMTP_PORT',
                    'placeholder': '587',
                    'required': True,
                },
                'from_email': {
                    'label': 'From Email',
                    'type': 'email',
                    'stored_key': 'SMTP_FROM_EMAIL',
                    'placeholder': 'noreply@example.com',
                    'required': True,
                },
                'from_name': {
                    'label': 'From Name',
                    'type': 'text',
                    'stored_key': 'SMTP_FROM_NAME',
                    'placeholder': 'Your Company',
                    'required': False,
                },
            },
            'secret_fields': {
                'username': {
                    'label': 'SMTP Username',
                    'type': 'text',
                    'stored_key': 'SMTP_USERNAME',
                    'placeholder': 'user@example.com',
                    'required': True,
                },
                'password': {
                    'label': 'SMTP Password',
                    'type': 'password',
                    'stored_key': 'SMTP_PASSWORD',
                    'placeholder': 'your smtp password',
                    'required': True,
                },
            },
        },

        # ── SMS ───────────────────────────────────────────────────────────────
        'twilio': {
            'slug': 'twilio',
            'display_name': 'Twilio SMS & Voice',
            'category': 'SMS & Voice',
            'icon': '📱',
            'description': 'Send SMS campaigns, manage inbox, and handle voice calls',
            'color': '#f22f46',
            'config_fields': {
                'phone_number': {
                    'label': 'Twilio Phone Number',
                    'type': 'text',
                    'stored_key': 'twilio_phone_number',
                    'placeholder': '+15551234567',
                    'required': True,
                    'help_text': 'The Twilio number you purchased — in E.164 format',
                },
            },
            'secret_fields': {
                'account_sid': {
                    'label': 'Account SID',
                    'type': 'text',
                    'stored_key': 'twilio_account_sid',
                    'placeholder': 'ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
                    'required': True,
                    'help_text': 'console.twilio.com → Account Info',
                },
                'auth_token': {
                    'label': 'Auth Token',
                    'type': 'password',
                    'stored_key': 'twilio_auth_token',
                    'placeholder': 'auth_token...',
                    'required': True,
                    'help_text': 'console.twilio.com → Account Info (keep secret)',
                },
            },
        },

        # ── Analytics ─────────────────────────────────────────────────────────
        'google_analytics': {
            'slug': 'google_analytics',
            'display_name': 'Google Analytics 4',
            'category': 'Analytics',
            'icon': '📊',
            'description': 'Track website traffic, conversions, and audience insights',
            'color': '#e37400',
            'config_fields': {
                'property_id': {
                    'label': 'GA4 Property ID',
                    'type': 'text',
                    'stored_key': 'GA4_PROPERTY_ID',
                    'placeholder': '123456789',
                    'required': True,
                    'help_text': 'analytics.google.com → Admin → Property Settings',
                },
            },
            'secret_fields': {
                'service_account_json': {
                    'label': 'Service Account JSON',
                    'type': 'textarea',
                    'stored_key': 'GA4_SERVICE_ACCOUNT_JSON',
                    'placeholder': '{"type": "service_account", ...}',
                    'required': True,
                    'help_text': 'console.cloud.google.com → IAM → Service Accounts → Keys → Add Key → JSON',
                },
            },
        },

        # ── Advertising ───────────────────────────────────────────────────────
        'google_ads': {
            'slug': 'google_ads',
            'display_name': 'Google Ads',
            'category': 'Advertising',
            'icon': '🎯',
            'description': 'Manage Google Ads campaigns and track ad performance',
            'color': '#4285f4',
            'config_fields': {
                'customer_id': {
                    'label': 'Customer ID',
                    'type': 'text',
                    'stored_key': 'GOOGLE_ADS_CUSTOMER_ID',
                    'placeholder': '123-456-7890',
                    'required': True,
                    'help_text': 'ads.google.com → top-right account menu',
                },
            },
            'secret_fields': {
                'developer_token': {
                    'label': 'Developer Token',
                    'type': 'password',
                    'stored_key': 'GOOGLE_ADS_DEVELOPER_TOKEN',
                    'placeholder': 'developer_token...',
                    'required': True,
                    'help_text': 'ads.google.com → Tools → API Center',
                },
                'client_id': {
                    'label': 'OAuth Client ID',
                    'type': 'text',
                    'stored_key': 'GOOGLE_ADS_CLIENT_ID',
                    'placeholder': 'client_id...',
                    'required': True,
                },
                'client_secret': {
                    'label': 'OAuth Client Secret',
                    'type': 'password',
                    'stored_key': 'GOOGLE_ADS_CLIENT_SECRET',
                    'placeholder': 'client_secret...',
                    'required': True,
                },
                'refresh_token': {
                    'label': 'Refresh Token',
                    'type': 'password',
                    'stored_key': 'GOOGLE_ADS_REFRESH_TOKEN',
                    'placeholder': 'refresh_token...',
                    'required': True,
                    'help_text': 'Generated via OAuth consent flow',
                },
            },
        },

        'exoclick': {
            'slug': 'exoclick',
            'display_name': 'ExoClick',
            'category': 'Advertising',
            'icon': '📢',
            'description': 'Manage ExoClick ad network campaigns',
            'color': '#ff6600',
            'config_fields': {
                'api_base': {
                    'label': 'API Base URL',
                    'type': 'url',
                    'stored_key': 'EXOCLICK_API_BASE',
                    'placeholder': 'https://api.exoclick.com',
                    'required': True,
                },
            },
            'secret_fields': {
                'api_token': {
                    'label': 'API Token',
                    'type': 'password',
                    'stored_key': 'EXOCLICK_API_TOKEN',
                    'placeholder': 'token...',
                    'required': True,
                    'help_text': 'ui.exoclick.com → Account → API Token',
                },
            },
        },

        'clickadilla': {
            'slug': 'clickadilla',
            'display_name': 'ClickAdilla',
            'category': 'Advertising',
            'icon': '🖱️',
            'description': 'ClickAdilla ad network integration',
            'color': '#7c3aed',
            'config_fields': {},
            'secret_fields': {
                'api_token': {
                    'label': 'API Token',
                    'type': 'password',
                    'stored_key': 'CLICKADILLA_TOKEN',
                    'placeholder': 'token...',
                    'required': True,
                    'help_text': 'clickadilla.com → Account → API',
                },
            },
        },

        'tubecorporate': {
            'slug': 'tubecorporate',
            'display_name': 'TubeCorporate',
            'category': 'Advertising',
            'icon': '🎥',
            'description': 'TubeCorporate video advertising platform',
            'color': '#dc2626',
            'config_fields': {
                'campaign_id': {
                    'label': 'Campaign ID',
                    'type': 'text',
                    'stored_key': 'TUBECORPORATE_CAMPAIGN_ID',
                    'placeholder': 'campaign_id...',
                    'required': False,
                },
                'promo': {
                    'label': 'Promo Code',
                    'type': 'text',
                    'stored_key': 'TUBECORPORATE_PROMO',
                    'placeholder': 'promo...',
                    'required': False,
                },
                'dc': {
                    'label': 'DC',
                    'type': 'text',
                    'stored_key': 'TUBECORPORATE_DC',
                    'placeholder': 'dc...',
                    'required': False,
                },
                'mc': {
                    'label': 'MC',
                    'type': 'text',
                    'stored_key': 'TUBECORPORATE_MC',
                    'placeholder': 'mc...',
                    'required': False,
                },
                'tc': {
                    'label': 'TC',
                    'type': 'text',
                    'stored_key': 'TUBECORPORATE_TC',
                    'placeholder': 'tc...',
                    'required': False,
                },
            },
            'secret_fields': {},
        },

        # ── E-commerce ────────────────────────────────────────────────────────
        'woocommerce': {
            'slug': 'woocommerce',
            'display_name': 'WooCommerce',
            'category': 'E-commerce',
            'icon': '🛒',
            'description': 'Sync products, orders, and customers from your WooCommerce store',
            'color': '#96588a',
            'config_fields': {
                'store_url': {
                    'label': 'Store URL',
                    'type': 'url',
                    'stored_key': 'WC_STORE_URL',
                    'placeholder': 'https://your-store.com',
                    'required': True,
                },
            },
            'secret_fields': {
                'consumer_key': {
                    'label': 'Consumer Key',
                    'type': 'password',
                    'stored_key': 'WC_CONSUMER_KEY',
                    'placeholder': 'ck_...',
                    'required': True,
                    'help_text': 'WooCommerce → Settings → Advanced → REST API',
                },
                'consumer_secret': {
                    'label': 'Consumer Secret',
                    'type': 'password',
                    'stored_key': 'WC_CONSUMER_SECRET',
                    'placeholder': 'cs_...',
                    'required': True,
                },
            },
        },

        # ── Automation ────────────────────────────────────────────────────────
        'zapier': {
            'slug': 'zapier',
            'display_name': 'Zapier',
            'category': 'Automation',
            'icon': '⚡',
            'description': 'Receive contacts and trigger workflows via Zapier webhooks',
            'color': '#ff4a00',
            'config_fields': {},
            'secret_fields': {
                'webhook_secret': {
                    'label': 'Webhook Secret',
                    'type': 'password',
                    'stored_key': 'zapier_webhook_secret',
                    'placeholder': 'your_secret...',
                    'required': True,
                    'help_text': 'Set any secret string — must match what you configure in your Zap',
                },
            },
        },

        'n8n': {
            'slug': 'n8n',
            'display_name': 'n8n Automation',
            'category': 'Automation',
            'icon': '🔀',
            'description': 'Trigger n8n workflows on lifecycle events (signup, billing, etc.)',
            'color': '#ea4b71',
            'config_fields': {
                'webhook_url': {
                    'label': 'Webhook URL',
                    'type': 'url',
                    'stored_key': 'N8N_WEBHOOK_URL',
                    'placeholder': 'https://your-n8n.example.com/webhook/luxit',
                    'required': True,
                    'help_text': 'The n8n webhook URL that receives LUXit events',
                },
            },
            'secret_fields': {
                'api_key': {
                    'label': 'API Key (optional)',
                    'type': 'password',
                    'stored_key': 'N8N_API_KEY',
                    'placeholder': 'n8n-api-...',
                    'required': False,
                    'help_text': 'Required only if your webhook uses header-based auth',
                },
            },
        },

        # ── Billing & Payments ────────────────────────────────────────────────
        'stripe': {
            'slug': 'stripe',
            'display_name': 'Stripe',
            'category': 'Billing & Payments',
            'icon': '💳',
            'description': 'Subscription billing, checkout sessions, and payment processing',
            'color': '#635bff',
            'config_fields': {
                'publishable_key': {
                    'label': 'Publishable Key',
                    'type': 'text',
                    'stored_key': 'STRIPE_PUBLISHABLE_KEY',
                    'placeholder': 'pk_live_...',
                    'required': False,
                    'help_text': 'Used in frontend for Stripe.js checkout',
                },
                'webhook_endpoint': {
                    'label': 'Webhook Endpoint (info only)',
                    'type': 'text',
                    'stored_key': 'STRIPE_WEBHOOK_ENDPOINT',
                    'placeholder': 'https://luxit.app/api/stripe/webhook',
                    'required': False,
                    'help_text': 'Register this URL in your Stripe dashboard',
                },
            },
            'secret_fields': {
                'secret_key': {
                    'label': 'Secret Key',
                    'type': 'password',
                    'stored_key': 'STRIPE_SECRET_KEY',
                    'placeholder': 'sk_live_...',
                    'required': True,
                    'help_text': 'dashboard.stripe.com → Developers → API keys',
                },
                'webhook_secret': {
                    'label': 'Webhook Signing Secret',
                    'type': 'password',
                    'stored_key': 'STRIPE_WEBHOOK_SECRET',
                    'placeholder': 'whsec_...',
                    'required': False,
                    'help_text': 'dashboard.stripe.com → Developers → Webhooks → your endpoint',
                },
            },
        },

        'mypaylink': {
            'slug': 'mypaylink',
            'display_name': 'MyPayLink',
            'category': 'Billing & Payments',
            'icon': '🔗',
            'description': 'Payment links, payroll, and payout management',
            'color': '#059669',
            'config_fields': {
                'api_url': {
                    'label': 'API Base URL',
                    'type': 'url',
                    'stored_key': 'MYPAYLINK_API_URL',
                    'placeholder': 'https://app.mypaylink.app/api',
                    'required': True,
                },
                'account_id': {
                    'label': 'Account ID',
                    'type': 'text',
                    'stored_key': 'MYPAYLINK_ACCOUNT_ID',
                    'placeholder': 'account_id...',
                    'required': False,
                },
            },
            'secret_fields': {
                'api_key': {
                    'label': 'API Key',
                    'type': 'password',
                    'stored_key': 'MYPAYLINK_API_KEY',
                    'placeholder': 'api_key...',
                    'required': True,
                    'help_text': 'MyPayLink dashboard → API Settings',
                },
            },
        },

        # ── Database & Auth ───────────────────────────────────────────────────
        'supabase': {
            'slug': 'supabase',
            'display_name': 'Supabase',
            'category': 'Database & Auth',
            'icon': '🗄️',
            'description': 'User identity and tenant database via Supabase',
            'color': '#3ecf8e',
            'config_fields': {
                'project_url': {
                    'label': 'Project URL',
                    'type': 'url',
                    'stored_key': 'SUPABASE_PROJECT_URL',
                    'placeholder': 'https://xxxx.supabase.co',
                    'required': True,
                },
            },
            'secret_fields': {
                'anon_key': {
                    'label': 'Anon / Public Key',
                    'type': 'password',
                    'stored_key': 'SUPABASE_ANON_KEY',
                    'placeholder': 'eyJh...',
                    'required': False,
                    'help_text': 'Safe for frontend use',
                },
                'service_role_key': {
                    'label': 'Service Role Key',
                    'type': 'password',
                    'stored_key': 'SUPABASE_SERVICE_ROLE_KEY',
                    'placeholder': 'eyJh...',
                    'required': True,
                    'help_text': 'Keep secret — server-side tenant management only',
                },
            },
        },

        # ── SEO ───────────────────────────────────────────────────────────────
        'dataforseo': {
            'slug': 'dataforseo',
            'display_name': 'DataForSEO',
            'category': 'SEO & Search',
            'icon': '🔍',
            'description': 'Keyword research, SERP data, and site audit via DataForSEO',
            'color': '#00d4aa',
            'config_fields': {
                'login': {
                    'label': 'Login (Email)',
                    'type': 'text',
                    'stored_key': 'dataforseo_login',
                    'placeholder': 'your_login@example.com',
                    'required': True,
                    'help_text': 'app.dataforseo.com → API Access',
                },
            },
            'secret_fields': {
                'password': {
                    'label': 'Password',
                    'type': 'password',
                    'stored_key': 'dataforseo_password',
                    'placeholder': 'your_password...',
                    'required': True,
                },
            },
        },

        'semrush': {
            'slug': 'semrush',
            'display_name': 'SEMrush',
            'category': 'SEO & Search',
            'icon': '📈',
            'description': 'Keyword analytics, competitor research, and backlink data',
            'color': '#ff6b35',
            'config_fields': {},
            'secret_fields': {
                'api_key': {
                    'label': 'API Key',
                    'type': 'password',
                    'stored_key': 'semrush_api_key',
                    'placeholder': 'api_key...',
                    'required': True,
                    'help_text': 'semrush.com → Account → Subscription → API',
                },
            },
        },

        'moz': {
            'slug': 'moz',
            'display_name': 'Moz',
            'category': 'SEO & Search',
            'icon': '🦎',
            'description': 'Domain Authority, link metrics, and keyword difficulty',
            'color': '#3b82f6',
            'config_fields': {
                'access_id': {
                    'label': 'Access ID',
                    'type': 'text',
                    'stored_key': 'moz_access_id',
                    'placeholder': 'access_id...',
                    'required': True,
                    'help_text': 'moz.com/products/api/keys',
                },
            },
            'secret_fields': {
                'secret_key': {
                    'label': 'Secret Key',
                    'type': 'password',
                    'stored_key': 'moz_secret_key',
                    'placeholder': 'secret_key...',
                    'required': True,
                },
            },
        },

        # ── Events ────────────────────────────────────────────────────────────
        'eventbrite': {
            'slug': 'eventbrite',
            'display_name': 'Eventbrite',
            'category': 'Events',
            'icon': '🎫',
            'description': 'Sync and promote events from Eventbrite',
            'color': '#f05537',
            'config_fields': {},
            'secret_fields': {
                'api_key': {
                    'label': 'Private Token',
                    'type': 'password',
                    'stored_key': 'eventbrite_api_key',
                    'placeholder': 'private_token...',
                    'required': True,
                    'help_text': 'eventbrite.com → Account Settings → Developer Links → API Keys',
                },
            },
        },

        'ticketmaster': {
            'slug': 'ticketmaster',
            'display_name': 'Ticketmaster',
            'category': 'Events',
            'icon': '🎟️',
            'description': 'Discover and promote Ticketmaster events',
            'color': '#026cdf',
            'config_fields': {},
            'secret_fields': {
                'api_key': {
                    'label': 'Discovery API Key',
                    'type': 'password',
                    'stored_key': 'ticketmaster_api_key',
                    'placeholder': 'api_key...',
                    'required': True,
                    'help_text': 'developer.ticketmaster.com → My Apps → Add New App',
                },
            },
        },

        # ── Media ─────────────────────────────────────────────────────────────
        'unsplash': {
            'slug': 'unsplash',
            'display_name': 'Unsplash',
            'category': 'Media & Images',
            'icon': '🖼️',
            'description': 'Search and use royalty-free images in campaigns',
            'color': '#111111',
            'config_fields': {},
            'secret_fields': {
                'access_key': {
                    'label': 'Access Key',
                    'type': 'password',
                    'stored_key': 'UNSPLASH_ACCESS_KEY',
                    'placeholder': 'access_key...',
                    'required': True,
                    'help_text': 'unsplash.com/developers → Your Applications → New Application',
                },
            },
        },

        'pexels': {
            'slug': 'pexels',
            'display_name': 'Pexels',
            'category': 'Media & Images',
            'icon': '📷',
            'description': 'Free stock photos and videos for campaign creatives',
            'color': '#05a081',
            'config_fields': {},
            'secret_fields': {
                'api_key': {
                    'label': 'API Key',
                    'type': 'password',
                    'stored_key': 'PEXELS_API_KEY',
                    'placeholder': 'api_key...',
                    'required': True,
                    'help_text': 'pexels.com/api/new/ → request API access',
                },
            },
        },

        # ── URL & Short Links ─────────────────────────────────────────────────
        'bitly': {
            'slug': 'bitly',
            'display_name': 'Bitly',
            'category': 'URL Shortening',
            'icon': '🔗',
            'description': 'Shorten and track campaign links with Bitly',
            'color': '#ee6123',
            'config_fields': {},
            'secret_fields': {
                'access_token': {
                    'label': 'Access Token',
                    'type': 'password',
                    'stored_key': 'BITLY_ACCESS_TOKEN',
                    'placeholder': 'access_token...',
                    'required': True,
                    'help_text': 'bitly.com → Account → Developer → Access Token',
                },
            },
        },

        # ── Developer ─────────────────────────────────────────────────────────
        'airtable': {
            'slug': 'airtable',
            'display_name': 'Airtable',
            'category': 'Developer & Data',
            'icon': '📋',
            'description': 'Sync leads and onboarding data with Airtable bases',
            'color': '#fcb400',
            'config_fields': {
                'base_id': {
                    'label': 'Base ID',
                    'type': 'text',
                    'stored_key': 'AIRTABLE_BASE_ID',
                    'placeholder': 'appXXXXXXXXXXXXXX',
                    'required': True,
                    'help_text': 'airtable.com/developers/web/api → select your base',
                },
                'leads_table': {
                    'label': 'Leads Table Name',
                    'type': 'text',
                    'stored_key': 'AIRTABLE_LEADS_TABLE',
                    'placeholder': 'Leads',
                    'required': False,
                },
            },
            'secret_fields': {
                'api_key': {
                    'label': 'Personal Access Token',
                    'type': 'password',
                    'stored_key': 'AIRTABLE_API_KEY',
                    'placeholder': 'pat...',
                    'required': True,
                    'help_text': 'airtable.com/create/tokens → Create new token',
                },
            },
        },

        'github': {
            'slug': 'github',
            'display_name': 'GitHub',
            'category': 'Developer & Data',
            'icon': '🐙',
            'description': 'Manage repos, issues, and pull requests (platform admin only)',
            'color': '#24292f',
            'config_fields': {},
            'secret_fields': {
                'personal_access_token': {
                    'label': 'Personal Access Token',
                    'type': 'password',
                    'stored_key': 'GITHUB_PERSONAL_ACCESS_TOKEN',
                    'placeholder': 'ghp_...',
                    'required': True,
                    'help_text': 'github.com → Settings → Developer settings → Personal access tokens',
                },
            },
        },

        'revenuecat': {
            'slug': 'revenuecat',
            'display_name': 'RevenueCat',
            'category': 'Developer & Data',
            'icon': '💰',
            'description': 'Subscription management and entitlement tracking',
            'color': '#e85d00',
            'config_fields': {},
            'secret_fields': {
                'secret_key': {
                    'label': 'Secret Key',
                    'type': 'password',
                    'stored_key': 'REVENUECAT_SECRET_KEY',
                    'placeholder': 'sk_...',
                    'required': True,
                    'help_text': 'app.revenuecat.com → Project → API Keys',
                },
            },
        },
    }

    # Ordered category list for display
    CATEGORY_ORDER = [
        'AI & Content',
        'Social Media',
        'Email & Calendar',
        'SMS & Voice',
        'Analytics',
        'Advertising',
        'E-commerce',
        'Automation',
        'Billing & Payments',
        'Database & Auth',
        'SEO & Search',
        'Events',
        'Media & Images',
        'URL Shortening',
        'Developer & Data',
    ]

    @classmethod
    def get_service(cls, slug):
        return cls.SERVICES.get(slug)

    @classmethod
    def get_all_services(cls):
        return cls.SERVICES

    @classmethod
    def get_services_by_category(cls):
        categories = {}
        for slug, service in cls.SERVICES.items():
            cat = service.get('category', 'Other')
            categories.setdefault(cat, [])
            categories[cat].append(service)
        # Return in preferred order
        ordered = {}
        for cat in cls.CATEGORY_ORDER:
            if cat in categories:
                ordered[cat] = categories[cat]
        for cat, svcs in categories.items():
            if cat not in ordered:
                ordered[cat] = svcs
        return ordered

    @classmethod
    def all_stored_keys(cls):
        """Return a flat list of every stored_key across all providers."""
        keys = []
        for svc in cls.SERVICES.values():
            for fdef in svc.get('config_fields', {}).values():
                if 'stored_key' in fdef:
                    keys.append(fdef['stored_key'])
            for fdef in svc.get('secret_fields', {}).values():
                if 'stored_key' in fdef:
                    keys.append(fdef['stored_key'])
        return keys

    @classmethod
    def validate_config(cls, slug, config, secrets):
        service = cls.get_service(slug)
        if not service:
            return False, f"Unknown service: {slug}"
        errors = []
        for field_name, field_def in service.get('config_fields', {}).items():
            if field_def.get('required') and not config.get(field_name):
                errors.append(f"{field_def['label']} is required")
        for field_name, field_def in service.get('secret_fields', {}).items():
            if field_def.get('required') and not secrets.get(field_name):
                errors.append(f"{field_def['label']} is required")
        if errors:
            return False, '; '.join(errors)
        return True, "Configuration is valid"


registry = IntegrationServiceRegistry()
