"""
Advanced Configuration Blueprint
Handles company-specific integration configurations
"""
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for
from flask_login import login_required, current_user
from extensions import db, csrf
from models import CompanyIntegrationConfig, IntegrationAuditLog
from services.secret_vault import vault
from services.integration_registry import registry
from datetime import datetime
import json
import logging

logger = logging.getLogger(__name__)

advanced_config_bp = Blueprint('advanced_config', __name__)

def require_company_access():
    """Ensure user has access to a company"""
    company = current_user.get_default_company()
    if not company:
        return None, jsonify({'error': 'No company access'}), 403
    return company, None, None

# /api/migrate-env-secrets/<company_id> was removed: it copied platform environment
# secrets (OpenAI, Google Ads, ExoClick, Twitter ...) into a tenant's integration config.
# Tenants enter their own credentials via /api/integrations/<service_slug>.

@advanced_config_bp.route('/settings/integrations')
@login_required
def integrations_list():
    """Display all integrations for the current company"""
    company = current_user.get_default_company()
    if not company:
        flash('No company access', 'error')
        return redirect(url_for('main.dashboard'))
    
    # Get all configured integrations for this company
    configured = CompanyIntegrationConfig.query.filter_by(
        company_id=company.id,
        is_active=True
    ).all()
    
    # Convert to dict for easy lookup
    configured_dict = {config.service_slug: config for config in configured}
    
    # Get all available services
    services_by_category = registry.get_services_by_category()
    
    return render_template(
        'advanced_configuration.html',
        services_by_category=services_by_category,
        configured=configured_dict,
        company=company
    )

@advanced_config_bp.route('/api/integrations/<service_slug>', methods=['GET'])
@login_required
def get_integration(service_slug):
    """Get integration configuration for a service"""
    company, error_response, code = require_company_access()
    if error_response:
        return error_response, code
    
    # Get service metadata
    service = registry.get_service(service_slug)
    if not service:
        return jsonify({'error': 'Unknown service'}), 404
    
    # Get configuration
    config = CompanyIntegrationConfig.query.filter_by(
        company_id=company.id,
        service_slug=service_slug
    ).first()
    
    if not config:
        # Return service metadata with empty config
        return jsonify({
            'service': service,
            'config': {},
            'secrets': {},
            'is_configured': False
        })
    
    # Decrypt secrets for display (masked)
    decrypted_secrets = {}
    if config.encrypted_secrets_json:
        try:
            encrypted_data = json.loads(config.encrypted_secrets_json)
            decrypted = vault.decrypt_dict(encrypted_data)
            # Mask secrets for display
            for key, value in decrypted.items():
                decrypted_secrets[key] = vault.mask_secret(value)
        except Exception as e:
            logger.error(f"Failed to decrypt secrets: {e}")
    
    return jsonify({
        'service': service,
        'config': config.config_json or {},
        'secrets': decrypted_secrets,
        'is_configured': True,
        'status': config.status,
        'test_status': config.test_status,
        'test_message': config.test_message,
        'last_tested_at': config.last_tested_at.isoformat() if config.last_tested_at else None,
        'updated_at': config.updated_at.isoformat() if config.updated_at else None
    })

@advanced_config_bp.route('/api/integrations/<service_slug>', methods=['POST', 'PUT'])
@login_required
def save_integration(service_slug):
    """Create or update integration configuration"""
    company, error_response, code = require_company_access()
    if error_response:
        return error_response, code
    
    # Get service metadata
    service = registry.get_service(service_slug)
    if not service:
        return jsonify({'error': 'Unknown service'}), 404
    
    # Get request data
    data = request.get_json()
    config_data = data.get('config', {})
    secrets_data = data.get('secrets', {})
    
    # Validate configuration
    is_valid, validation_message = registry.validate_config(
        service_slug,
        config_data,
        secrets_data
    )
    
    if not is_valid:
        return jsonify({'error': validation_message}), 400
    
    # Find or create configuration
    config = CompanyIntegrationConfig.query.filter_by(
        company_id=company.id,
        service_slug=service_slug
    ).first()
    
    if not config:
        config = CompanyIntegrationConfig(
            company_id=company.id,
            service_slug=service_slug,
            display_name=service['display_name'],
            created_by_id=current_user.id
        )
        action = 'created'
    else:
        action = 'updated'
    
    # Store non-sensitive config
    config.config_json = config_data
    
    # Encrypt and store secrets
    if secrets_data:
        # Only encrypt non-masked values (new/changed secrets)
        secrets_to_encrypt = {}
        for key, value in secrets_data.items():
            # Skip if value is masked (unchanged)
            if value and not value.startswith('***'):
                secrets_to_encrypt[key] = value
        
        if secrets_to_encrypt:
            encrypted_data = vault.encrypt_dict(secrets_to_encrypt)
            
            # Merge with existing secrets if updating
            if config.encrypted_secrets_json:
                try:
                    existing = json.loads(config.encrypted_secrets_json)
                    existing.update(encrypted_data)
                    config.encrypted_secrets_json = json.dumps(existing)
                except:
                    config.encrypted_secrets_json = json.dumps(encrypted_data)
            else:
                config.encrypted_secrets_json = json.dumps(encrypted_data)
    
    config.updated_by_id = current_user.id
    config.updated_at = datetime.utcnow()
    
    if not config.id:
        db.session.add(config)
    
    # Create audit log
    audit = IntegrationAuditLog(
        company_id=company.id,
        config_id=config.id if config.id else None,
        service_slug=service_slug,
        action=action,
        user_id=current_user.id,
        changes={'config_fields': list(config_data.keys()), 'secret_fields': list(secrets_data.keys())},
        ip_address=request.remote_addr
    )
    db.session.add(audit)
    
    try:
        db.session.commit()
        return jsonify({
            'success': True,
            'message': f'Integration {action} successfully',
            'config_id': config.id
        })
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to save integration: {e}")
        return jsonify({'error': str(e)}), 500

@advanced_config_bp.route('/api/integrations/<service_slug>/test', methods=['POST'])
@login_required
def test_integration(service_slug):
    """Test integration connection"""
    company, error_response, code = require_company_access()
    if error_response:
        return error_response, code
    
    # Get configuration
    config = CompanyIntegrationConfig.query.filter_by(
        company_id=company.id,
        service_slug=service_slug
    ).first()
    
    if not config:
        return jsonify({'error': 'Integration not configured'}), 404
    
    # Decrypt secrets for testing
    try:
        config_data = config.config_json or {}
        secrets_data = {}
        
        if config.encrypted_secrets_json:
            encrypted_data = json.loads(config.encrypted_secrets_json)
            secrets_data = vault.decrypt_dict(encrypted_data)
        
        # Test based on service type
        test_result = test_service_connection(service_slug, config_data, secrets_data)
        
        # Update test status
        config.last_tested_at = datetime.utcnow()
        config.test_status = 'success' if test_result['success'] else 'failed'
        config.test_message = test_result['message']
        
        # Create audit log
        audit = IntegrationAuditLog(
            company_id=company.id,
            config_id=config.id,
            service_slug=service_slug,
            action='tested',
            user_id=current_user.id,
            changes={'test_result': test_result['success']},
            ip_address=request.remote_addr
        )
        db.session.add(audit)
        db.session.commit()
        
        return jsonify(test_result)
        
    except Exception as e:
        logger.error(f"Test failed: {e}")
        return jsonify({
            'success': False,
            'message': f'Test failed: {str(e)}'
        }), 500

@advanced_config_bp.route('/api/integrations/<service_slug>', methods=['DELETE'])
@login_required
def delete_integration(service_slug):
    """Delete integration configuration"""
    company, error_response, code = require_company_access()
    if error_response:
        return error_response, code
    
    config = CompanyIntegrationConfig.query.filter_by(
        company_id=company.id,
        service_slug=service_slug
    ).first()
    
    if not config:
        return jsonify({'error': 'Integration not found'}), 404
    
    # Soft delete
    config.is_active = False
    config.status = 'deleted'
    config.updated_by_id = current_user.id
    
    # Create audit log
    audit = IntegrationAuditLog(
        company_id=company.id,
        config_id=config.id,
        service_slug=service_slug,
        action='deleted',
        user_id=current_user.id,
        changes={},
        ip_address=request.remote_addr
    )
    db.session.add(audit)
    db.session.commit()
    
    return jsonify({'success': True, 'message': 'Integration deleted'})


def test_service_connection(service_slug, config, secrets):
    """Test connection to a service"""
    # Basic connection testing - can be expanded per service
    
    if service_slug == 'openai':
        try:
            import openai
            client = openai.OpenAI(api_key=secrets.get('api_key'))
            # Simple test
            models = client.models.list()
            return {'success': True, 'message': 'Connection successful'}
        except Exception as e:
            return {'success': False, 'message': str(e)}
    
    elif service_slug == 'woocommerce':
        try:
            from woocommerce import API
            wcapi = API(
                url=config.get('store_url'),
                consumer_key=secrets.get('consumer_key'),
                consumer_secret=secrets.get('consumer_secret'),
                version="wc/v3"
            )
            # Test connection
            response = wcapi.get("products", params={"per_page": 1})
            if response.status_code == 200:
                return {'success': True, 'message': 'Connection successful'}
            else:
                return {'success': False, 'message': f'API returned status {response.status_code}'}
        except Exception as e:
            return {'success': False, 'message': str(e)}
    
    # Default response for untested services
    return {'success': True, 'message': 'Configuration saved (connection test not implemented)'}
