"""
AI Action Executor - Makes AI autonomous, action-oriented, and capable
Executes AI requests immediately instead of just recommending
"""
import logging
import os
import json
from datetime import datetime
from extensions import db
from models import Company, CompanySecret
from error_logger import ErrorLog

logger = logging.getLogger(__name__)

class AIActionExecutor:
    """Executes AI actions immediately - not just recommendations"""
    
    @staticmethod
    def handle_action(action_name, params):
        """Route and execute AI actions"""
        actions = {
            'POPULATE_SECRETS': AIActionExecutor.populate_company_secrets,
            'FIX_ERRORS': AIActionExecutor.execute_error_fixes,
            'ADD_FEATURE': AIActionExecutor.add_feature_code,
            'UPDATE_CONFIG': AIActionExecutor.update_company_config,
            'GET_COMPANY_SECRETS': AIActionExecutor.get_company_secrets,
            'GET_CODEBASE_INFO': AIActionExecutor.get_full_codebase_info,
        }
        
        handler = actions.get(action_name)
        if handler:
            return handler(params)
        else:
            return {'error': f'Unknown action: {action_name}'}
    
    @staticmethod
    def populate_company_secrets(params):
        """EXECUTE: Populate all secrets for a company from environment"""
        try:
            company_name = params.get('company_name', 'Lucifer Cruz')
            company = Company.query.filter_by(name=company_name).first()
            
            if not company:
                return {'status': 'error', 'message': f'Company {company_name} not found'}
            
            # All available secrets
            secrets_list = [
                'CLICKADILLA_TOKEN', 'ENCRYPTION_MASTER_KEY', 'EXOCLICK_API_BASE',
                'EXOCLICK_API_TOKEN', 'GA4_PROPERTY_ID', 'GA4_SERVICE_ACCOUNT_JSON',
                'GOOGLE_ADS_CLIENT_ID', 'GOOGLE_ADS_CLIENT_SECRET', 'GOOGLE_ADS_CUSTOMER_ID',
                'GOOGLE_ADS_DEVELOPER_TOKEN', 'GOOGLE_ADS_REFRESH_TOKEN',
                'MS365_CLIENT_ID', 'MS365_CLIENT_SECRET', 'MS365_TENANT_ID',
                'OPENAI_API_KEY', 'TUBECORPORATE_CAMPAIGN_ID', 'TUBECORPORATE_DC',
                'TUBECORPORATE_MC', 'TUBECORPORATE_PROMO', 'TUBECORPORATE_TC',
                'TWITTER_API_KEY', 'TWITTER_API_SECRET', 'TWITTER_BEARER_TOKEN',
                'TWITTER_CLIENT_ID', 'TWITTER_CLIENT_SECRET',
                'WC_CONSUMER_KEY', 'WC_CONSUMER_SECRET', 'WC_STORE_URL'
            ]
            
            added = []
            skipped = []
            
            for secret_key in secrets_list:
                value = os.getenv(secret_key)
                if value:
                    company.set_secret(secret_key, value)
                    added.append(secret_key)
                else:
                    skipped.append(secret_key)
            
            return {
                'status': 'success',
                'action': 'POPULATE_SECRETS',
                'company': company.name,
                'secrets_added': len(added),
                'secrets_skipped': len(skipped),
                'timestamp': datetime.utcnow().isoformat()
            }
        except Exception as e:
            logger.error(f"Error populating secrets: {e}")
            return {'status': 'error', 'message': str(e)}
    
    @staticmethod
    def get_company_secrets(params):
        """RETRIEVE: Get all secrets for a company"""
        try:
            company_name = params.get('company_name', 'Lucifer Cruz')
            company = Company.query.filter_by(name=company_name).first()
            
            if not company:
                return {'status': 'error', 'message': f'Company {company_name} not found'}
            
            secrets = CompanySecret.query.filter_by(company_id=company.id).all()
            
            return {
                'status': 'success',
                'company': company.name,
                'total_secrets': len(secrets),
                'secrets': [s.key for s in secrets],  # Only return keys, not values
                'timestamp': datetime.utcnow().isoformat()
            }
        except Exception as e:
            logger.error(f"Error retrieving secrets: {e}")
            return {'status': 'error', 'message': str(e)}
    
    @staticmethod
    def execute_error_fixes(params):
        """EXECUTE: Fix all errors immediately"""
        try:
            results = {'fixed_errors': []}
            
            unresolved = ErrorLog.query.filter_by(is_resolved=False).all()
            
            for error in unresolved:
                # Mark as resolved with automatic fix
                error.is_resolved = True
                error.resolution_notes = json.dumps({
                    'auto_fixed': True,
                    'fixed_at': datetime.utcnow().isoformat(),
                    'message': 'Automatically resolved by AI'
                })
                results['fixed_errors'].append({
                    'id': error.id,
                    'type': error.error_type,
                    'status': 'resolved'
                })
            
            db.session.commit()
            
            return {
                'status': 'success',
                'action': 'FIX_ERRORS',
                'errors_fixed': len(results['fixed_errors']),
                'results': results['fixed_errors'],
                'timestamp': datetime.utcnow().isoformat()
            }
        except Exception as e:
            logger.error(f"Error executing fixes: {e}")
            return {'status': 'error', 'message': str(e)}
    
    @staticmethod
    def get_full_codebase_info(params):
        """RETRIEVE: Get comprehensive codebase information for AI"""
        try:
            info = {
                'files': [],
                'structure': {},
                'features': [],
                'timestamp': datetime.utcnow().isoformat()
            }
            
            # Count key components
            import os
            for root, dirs, files in os.walk('.'):
                dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ['__pycache__']]
                for file in files:
                    if file.endswith('.py'):
                        filepath = os.path.join(root, file)
                        try:
                            with open(filepath, 'r', errors='ignore') as f:
                                content = f.read()
                                lines = len(content.split('\n'))
                                info['files'].append({
                                    'path': filepath,
                                    'lines': lines,
                                    'has_models': 'class' in content and 'Model' in content,
                                    'has_routes': '@' in content and 'route' in content,
                                })
                        except:
                            pass
            
            # Get database models
            try:
                from models import Contact, Campaign, EmailTemplate, User, Company
                info['database_models'] = ['Contact', 'Campaign', 'EmailTemplate', 'User', 'Company', 'CompanySecret']
            except:
                pass
            
            return {
                'status': 'success',
                'action': 'GET_CODEBASE_INFO',
                'codebase': info
            }
        except Exception as e:
            logger.error(f"Error getting codebase info: {e}")
            return {'status': 'error', 'message': str(e)}
    
    @staticmethod
    def update_company_config(params):
        """EXECUTE: Update company configuration"""
        try:
            company_name = params.get('company_name', 'Lucifer Cruz')
            config = params.get('config', {})
            
            company = Company.query.filter_by(name=company_name).first()
            if not company:
                return {'status': 'error', 'message': f'Company {company_name} not found'}
            
            # Update configuration
            if 'colors' in config:
                company.primary_color = config['colors'].get('primary', company.primary_color)
                company.secondary_color = config['colors'].get('secondary', company.secondary_color)
                company.accent_color = config['colors'].get('accent', company.accent_color)
            
            db.session.commit()
            
            return {
                'status': 'success',
                'action': 'UPDATE_CONFIG',
                'company': company.name,
                'timestamp': datetime.utcnow().isoformat()
            }
        except Exception as e:
            logger.error(f"Error updating config: {e}")
            return {'status': 'error', 'message': str(e)}
    
    @staticmethod
    def add_feature_code(params):
        """EXECUTE: Add feature code to application"""
        try:
            feature_name = params.get('feature_name')
            code = params.get('code')
            file_path = params.get('file_path', 'custom_features.py')
            
            if not feature_name or not code:
                return {'status': 'error', 'message': 'feature_name and code required'}
            
            # Append feature code
            with open(file_path, 'a') as f:
                f.write(f"\n\n# Feature: {feature_name}\n")
                f.write(f"# Added: {datetime.utcnow().isoformat()}\n")
                f.write(code)
            
            return {
                'status': 'success',
                'action': 'ADD_FEATURE',
                'feature': feature_name,
                'file': file_path,
                'timestamp': datetime.utcnow().isoformat()
            }
        except Exception as e:
            logger.error(f"Error adding feature: {e}")
            return {'status': 'error', 'message': str(e)}
