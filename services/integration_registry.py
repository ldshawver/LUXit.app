"""
Integration Service Registry
Defines metadata for all supported integrations
"""

class IntegrationServiceRegistry:
    """Registry of all supported integration services"""
    
    SERVICES = {
        'openai': {
            'slug': 'openai',
            'display_name': 'OpenAI / GPT',
            'category': 'AI & Content',
            'icon': 'cpu',
            'description': 'AI-powered content generation and chatbot',
            'config_fields': {},
            'secret_fields': {
                'api_key': {
                    'label': 'API Key',
                    'type': 'password',
                    'placeholder': 'sk-...',
                    'required': True,
                    'help_text': 'Your OpenAI API key'
                }
            }
        },
        
        'google_ads': {
            'slug': 'google_ads',
            'display_name': 'Google Ads',
            'category': 'Advertising',
            'icon': 'target',
            'description': 'Google Ads campaign management',
            'config_fields': {
                'customer_id': {
                    'label': 'Customer ID',
                    'type': 'text',
                    'placeholder': '123-456-7890',
                    'required': True,
                    'help_text': 'Your Google Ads customer ID'
                }
            },
            'secret_fields': {
                'developer_token': {
                    'label': 'Developer Token',
                    'type': 'password',
                    'required': True
                },
                'client_id': {
                    'label': 'Client ID',
                    'type': 'password',
                    'required': True
                },
                'client_secret': {
                    'label': 'Client Secret',
                    'type': 'password',
                    'required': True
                },
                'refresh_token': {
                    'label': 'Refresh Token',
                    'type': 'password',
                    'required': True
                }
            }
        },
        
        'exoclick': {
            'slug': 'exoclick',
            'display_name': 'ExoClick',
            'category': 'Advertising',
            'icon': 'zap',
            'description': 'ExoClick ad network integration',
            'config_fields': {
                'api_base': {
                    'label': 'API Base URL',
                    'type': 'url',
                    'placeholder': 'https://api.exoclick.com',
                    'required': True
                }
            },
            'secret_fields': {
                'api_token': {
                    'label': 'API Token',
                    'type': 'password',
                    'required': True,
                    'help_text': 'Your ExoClick API token'
                }
            }
        },
        
        'clickadilla': {
            'slug': 'clickadilla',
            'display_name': 'ClickAdilla',
            'category': 'Advertising',
            'icon': 'mouse-pointer',
            'description': 'ClickAdilla ad network',
            'config_fields': {},
            'secret_fields': {
                'api_token': {
                    'label': 'API Token',
                    'type': 'password',
                    'required': True
                }
            }
        },
        
        'tubecorporate': {
            'slug': 'tubecorporate',
            'display_name': 'TubeCorporate',
            'category': 'Advertising',
            'icon': 'video',
            'description': 'TubeCorporate advertising platform',
            'config_fields': {
                'campaign_id': {
                    'label': 'Campaign ID',
                    'type': 'text',
                    'required': False
                },
                'promo': {
                    'label': 'Promo Code',
                    'type': 'text',
                    'required': False
                },
                'dc': {
                    'label': 'DC',
                    'type': 'text',
                    'required': False
                },
                'mc': {
                    'label': 'MC',
                    'type': 'text',
                    'required': False
                },
                'tc': {
                    'label': 'TC',
                    'type': 'text',
                    'required': False
                }
            },
            'secret_fields': {}
        },
        
        'woocommerce': {
            'slug': 'woocommerce',
            'display_name': 'WooCommerce',
            'category': 'E-commerce',
            'icon': 'shopping-cart',
            'description': 'WooCommerce store integration',
            'config_fields': {
                'store_url': {
                    'label': 'Store URL',
                    'type': 'url',
                    'placeholder': 'https://your-store.com',
                    'required': True,
                    'help_text': 'Your WooCommerce store URL'
                }
            },
            'secret_fields': {
                'consumer_key': {
                    'label': 'Consumer Key',
                    'type': 'password',
                    'required': True
                },
                'consumer_secret': {
                    'label': 'Consumer Secret',
                    'type': 'password',
                    'required': True
                }
            }
        },
        
        'twitter': {
            'slug': 'twitter',
            'display_name': 'Twitter / X',
            'category': 'Social Media',
            'icon': 'twitter',
            'description': 'Twitter API integration',
            'config_fields': {},
            'secret_fields': {
                'api_key': {
                    'label': 'API Key',
                    'type': 'password',
                    'required': True
                },
                'api_secret': {
                    'label': 'API Secret',
                    'type': 'password',
                    'required': True
                },
                'bearer_token': {
                    'label': 'Bearer Token',
                    'type': 'password',
                    'required': False
                },
                'client_id': {
                    'label': 'Client ID',
                    'type': 'password',
                    'required': False
                },
                'client_secret': {
                    'label': 'Client Secret',
                    'type': 'password',
                    'required': False
                }
            }
        },
        
        'google_analytics': {
            'slug': 'google_analytics',
            'display_name': 'Google Analytics 4',
            'category': 'Analytics',
            'icon': 'bar-chart-2',
            'description': 'Google Analytics 4 integration',
            'config_fields': {
                'property_id': {
                    'label': 'Property ID',
                    'type': 'text',
                    'placeholder': '123456789',
                    'required': True,
                    'help_text': 'Your GA4 property ID'
                }
            },
            'secret_fields': {
                'service_account_json': {
                    'label': 'Service Account JSON',
                    'type': 'textarea',
                    'required': True,
                    'help_text': 'Paste your service account JSON key here'
                }
            }
        },
        
        'ms365': {
            'slug': 'ms365',
            'display_name': 'Microsoft 365',
            'category': 'Email & Calendar',
            'icon': 'mail',
            'description': 'Microsoft 365 / Outlook integration',
            'config_fields': {
                'tenant_id': {
                    'label': 'Tenant ID',
                    'type': 'text',
                    'required': True
                }
            },
            'secret_fields': {
                'client_id': {
                    'label': 'Client ID',
                    'type': 'password',
                    'required': True
                },
                'client_secret': {
                    'label': 'Client Secret',
                    'type': 'password',
                    'required': True
                }
            }
        },
        
        'smtp': {
            'slug': 'smtp',
            'display_name': 'SMTP Email',
            'category': 'Email & Calendar',
            'icon': 'send',
            'description': 'Custom SMTP email server',
            'config_fields': {
                'host': {
                    'label': 'SMTP Host',
                    'type': 'text',
                    'placeholder': 'smtp.gmail.com',
                    'required': True
                },
                'port': {
                    'label': 'SMTP Port',
                    'type': 'number',
                    'placeholder': '587',
                    'required': True
                },
                'from_email': {
                    'label': 'From Email',
                    'type': 'email',
                    'placeholder': 'noreply@example.com',
                    'required': True
                },
                'from_name': {
                    'label': 'From Name',
                    'type': 'text',
                    'placeholder': 'Your Company',
                    'required': False
                },
                'use_tls': {
                    'label': 'Use TLS',
                    'type': 'checkbox',
                    'default': True,
                    'required': False
                }
            },
            'secret_fields': {
                'username': {
                    'label': 'SMTP Username',
                    'type': 'text',
                    'required': True
                },
                'password': {
                    'label': 'SMTP Password',
                    'type': 'password',
                    'required': True
                }
            }
        },

        'stripe': {
            'slug': 'stripe',
            'display_name': 'Stripe',
            'category': 'Billing & Payments',
            'icon': 'credit-card',
            'description': 'Stripe subscription billing and payment processing',
            'config_fields': {
                'publishable_key': {
                    'label': 'Publishable Key',
                    'type': 'text',
                    'placeholder': 'pk_live_...',
                    'required': False,
                    'help_text': 'Used in frontend for checkout'
                },
                'webhook_endpoint': {
                    'label': 'Webhook Endpoint',
                    'type': 'text',
                    'placeholder': 'https://luxit.app/api/stripe/webhook',
                    'required': False,
                    'help_text': 'Register this URL in your Stripe dashboard'
                }
            },
            'secret_fields': {
                'secret_key': {
                    'label': 'Secret Key',
                    'type': 'password',
                    'placeholder': 'sk_live_...',
                    'required': True,
                    'help_text': 'Your Stripe secret key'
                },
                'webhook_secret': {
                    'label': 'Webhook Signing Secret',
                    'type': 'password',
                    'placeholder': 'whsec_...',
                    'required': False,
                    'help_text': 'From Stripe webhook dashboard — used to verify incoming events'
                }
            }
        },

        'supabase': {
            'slug': 'supabase',
            'display_name': 'Supabase',
            'category': 'Database & Auth',
            'icon': 'database',
            'description': 'Supabase user identity and tenant database',
            'config_fields': {
                'project_url': {
                    'label': 'Project URL',
                    'type': 'url',
                    'placeholder': 'https://xxxx.supabase.co',
                    'required': True,
                    'help_text': 'Your Supabase project URL'
                }
            },
            'secret_fields': {
                'anon_key': {
                    'label': 'Anon / Public Key',
                    'type': 'password',
                    'required': False,
                    'help_text': 'Safe to use in frontend'
                },
                'service_role_key': {
                    'label': 'Service Role Key',
                    'type': 'password',
                    'required': True,
                    'help_text': 'Keep secret — used for server-side tenant management'
                }
            }
        },

        'n8n': {
            'slug': 'n8n',
            'display_name': 'n8n Automation',
            'category': 'Automation',
            'icon': 'git-merge',
            'description': 'n8n workflow automation engine — triggers on LUXit lifecycle events',
            'config_fields': {
                'webhook_url': {
                    'label': 'Webhook URL',
                    'type': 'url',
                    'placeholder': 'https://your-n8n.example.com/webhook/luxit',
                    'required': True,
                    'help_text': 'The n8n webhook URL that receives LUXit event triggers'
                }
            },
            'secret_fields': {
                'api_key': {
                    'label': 'API Key (optional)',
                    'type': 'password',
                    'required': False,
                    'help_text': 'n8n API key for authenticated webhook calls'
                }
            }
        },

        'mypaylink': {
            'slug': 'mypaylink',
            'display_name': 'MyPayLink',
            'category': 'Billing & Payments',
            'icon': 'link',
            'description': 'MyPayLink payment links, payroll, and payout management',
            'config_fields': {
                'api_url': {
                    'label': 'API Base URL',
                    'type': 'url',
                    'placeholder': 'https://app.mypaylink.app/api',
                    'required': True,
                    'help_text': 'MyPayLink API base URL'
                },
                'account_id': {
                    'label': 'Account ID',
                    'type': 'text',
                    'required': False,
                    'help_text': 'Your MyPayLink account identifier'
                }
            },
            'secret_fields': {
                'api_key': {
                    'label': 'API Key',
                    'type': 'password',
                    'required': True,
                    'help_text': 'Your MyPayLink API key'
                }
            }
        }
    }
    
    @classmethod
    def get_service(cls, slug):
        """Get service metadata by slug"""
        return cls.SERVICES.get(slug)
    
    @classmethod
    def get_all_services(cls):
        """Get all services"""
        return cls.SERVICES
    
    @classmethod
    def get_services_by_category(cls):
        """Get services organized by category"""
        categories = {}
        for slug, service in cls.SERVICES.items():
            category = service.get('category', 'Other')
            if category not in categories:
                categories[category] = []
            categories[category].append(service)
        return categories
    
    @classmethod
    def validate_config(cls, slug, config, secrets):
        """Validate configuration for a service"""
        service = cls.get_service(slug)
        if not service:
            return False, f"Unknown service: {slug}"
        
        errors = []
        
        # Validate config fields
        for field_name, field_def in service.get('config_fields', {}).items():
            if field_def.get('required') and not config.get(field_name):
                errors.append(f"{field_def['label']} is required")
        
        # Validate secret fields
        for field_name, field_def in service.get('secret_fields', {}).items():
            if field_def.get('required') and not secrets.get(field_name):
                errors.append(f"{field_def['label']} is required")
        
        if errors:
            return False, '; '.join(errors)
        
        return True, "Configuration is valid"


# Global registry instance
registry = IntegrationServiceRegistry()
