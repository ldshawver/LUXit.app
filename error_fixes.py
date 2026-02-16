"""
Targeted Error Fix Solutions
Addresses InternalServerError, NotFound, and OpenAIAuthenticationError
"""
import logging
import os
import requests
from datetime import datetime
from extensions import db
from error_logger import ErrorLog

logger = logging.getLogger(__name__)

class ErrorFixService:
    """Targeted fixes for specific error types"""
    
    @staticmethod
    def validate_openai_api_key():
        """Test if OpenAI API key is valid"""
        try:
            from openai import OpenAI
            
            api_key = os.environ.get('OPENAI_API_KEY') or os.getenv('OPENAI_API_KEY')
            if not api_key:
                return {
                    'valid': False,
                    'message': 'OPENAI_API_KEY not configured',
                    'diagnosis': 'API key not found in environment',
                    'fix': 'Set OPENAI_API_KEY environment variable'
                }
            
            # Test key by making a simple request
            client = OpenAI(api_key=api_key)
            
            # Try a minimal request
            try:
                response = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": "test"}],
                    temperature=0.1,
                    max_tokens=10,
                    timeout=5
                )
                return {
                    'valid': True,
                    'message': 'OpenAI API key is valid and working',
                    'test_at': datetime.utcnow().isoformat()
                }
            except Exception as api_error:
                error_str = str(api_error)
                if '401' in error_str or 'invalid' in error_str.lower():
                    return {
                        'valid': False,
                        'message': 'API key is invalid or expired',
                        'diagnosis': 'Authentication failed with current key',
                        'fix': 'Verify API key at https://platform.openai.com/account/api-keys',
                        'error': error_str[:100]
                    }
                else:
                    return {
                        'valid': False,
                        'message': 'API connection failed',
                        'diagnosis': str(api_error)[:100],
                        'fix': 'Check network connection and API status'
                    }
        except Exception as e:
            return {
                'valid': False,
                'message': f'Error testing API key: {str(e)}',
                'diagnosis': str(e)[:100]
            }
    
    @staticmethod
    def check_server_health():
        """Check server health and resource usage"""
        try:
            import psutil
            
            health = {
                'timestamp': datetime.utcnow().isoformat(),
                'status': 'healthy',
                'checks': {}
            }
            
            # CPU usage
            cpu_percent = psutil.cpu_percent(interval=1)
            health['checks']['cpu'] = {
                'percent': cpu_percent,
                'status': 'ok' if cpu_percent < 80 else 'warning' if cpu_percent < 95 else 'critical',
                'message': f'CPU usage: {cpu_percent}%'
            }
            
            # Memory usage
            memory = psutil.virtual_memory()
            health['checks']['memory'] = {
                'percent': memory.percent,
                'available_mb': memory.available / (1024**2),
                'status': 'ok' if memory.percent < 80 else 'warning' if memory.percent < 95 else 'critical',
                'message': f'Memory usage: {memory.percent}%'
            }
            
            # Disk usage
            try:
                disk = psutil.disk_usage('/')
                health['checks']['disk'] = {
                    'percent': disk.percent,
                    'free_gb': disk.free / (1024**3),
                    'status': 'ok' if disk.percent < 80 else 'warning' if disk.percent < 95 else 'critical',
                    'message': f'Disk usage: {disk.percent}%'
                }
            except:
                pass
            
            # Check if any warning/critical
            statuses = [check.get('status', 'ok') for check in health['checks'].values()]
            if 'critical' in statuses:
                health['status'] = 'critical'
            elif 'warning' in statuses:
                health['status'] = 'warning'
            
            return health
        except ImportError:
            logger.warning("psutil not available for system health check")
            return {
                'status': 'unknown',
                'message': 'System monitoring not available',
                'timestamp': datetime.utcnow().isoformat()
            }
        except Exception as e:
            logger.error(f"Error checking server health: {e}")
            return {
                'status': 'error',
                'message': str(e),
                'timestamp': datetime.utcnow().isoformat()
            }
    
    @staticmethod
    def diagnose_500_error():
        """Diagnose 500 Internal Server Error"""
        try:
            # Check server resources
            health = ErrorFixService.check_server_health()
            
            diagnosis = {
                'error_type': 'InternalServerError',
                'timestamp': datetime.utcnow().isoformat(),
                'server_health': health,
                'possible_causes': []
            }
            
            # Analyze health data
            if health['status'] == 'critical':
                diagnosis['possible_causes'].append('Server resource exhaustion (CPU/Memory/Disk)')
            
            # Check database connectivity
            try:
                from models import Contact
                Contact.query.first()
                diagnosis['database'] = 'connected'
            except Exception as db_error:
                diagnosis['database'] = f'error: {str(db_error)[:50]}'
                diagnosis['possible_causes'].append('Database connection issue')
            
            # Check recent errors
            try:
                recent_errors = ErrorLog.query.order_by(
                    ErrorLog.created_at.desc()
                ).limit(5).all()
                diagnosis['recent_errors'] = [e.error_type for e in recent_errors]
                diagnosis['possible_causes'].append('See recent errors in diagnostics')
            except:
                pass
            
            # Solutions
            diagnosis['solutions'] = [
                'Restart the application server',
                'Check server resource constraints',
                'Review recent code changes',
                'Check database connectivity',
                'Review application logs'
            ]
            
            return diagnosis
        except Exception as e:
            logger.error(f"Error diagnosing 500 error: {e}")
            return {
                'error_type': 'InternalServerError',
                'diagnosis_error': str(e)
            }
    
    @staticmethod
    def check_404_endpoints():
        """Check which endpoints are returning 404"""
        try:
            common_endpoints = [
                '/api/diagnostics/errors',
                '/api/diagnostics/health',
                '/api/auto-repair/start',
                '/api/auto-repair/clear',
                '/chatbot/send',
                '/dashboard',
                '/crm',
                '/campaigns',
            ]
            
            results = {
                'timestamp': datetime.utcnow().isoformat(),
                'endpoints': {},
                'issues': []
            }
            
            base_url = 'http://localhost:5000'
            
            for endpoint in common_endpoints:
                try:
                    response = requests.get(f"{base_url}{endpoint}", timeout=3)
                    status = 'ok' if response.status_code < 400 else 'error'
                    results['endpoints'][endpoint] = {
                        'status': status,
                        'code': response.status_code
                    }
                    
                    if response.status_code == 404:
                        results['issues'].append(f"{endpoint} returns 404")
                except requests.exceptions.Timeout:
                    results['endpoints'][endpoint] = {'status': 'timeout', 'code': None}
                    results['issues'].append(f"{endpoint} timeout")
                except Exception as e:
                    results['endpoints'][endpoint] = {'status': 'error', 'error': str(e)[:50]}
            
            return results
        except Exception as e:
            logger.error(f"Error checking 404 endpoints: {e}")
            return {'error': str(e)}
    
    @staticmethod
    def comprehensive_system_diagnosis():
        """Run comprehensive system diagnosis"""
        return {
            'timestamp': datetime.utcnow().isoformat(),
            'server_health': ErrorFixService.check_server_health(),
            'openai_validation': ErrorFixService.validate_openai_api_key(),
            'internal_server_error_diagnosis': ErrorFixService.diagnose_500_error(),
            '404_endpoint_check': ErrorFixService.check_404_endpoints(),
            'database_status': {
                'connected': True,
                'message': 'Database check passed'
            } if ErrorFixService._test_database() else {
                'connected': False,
                'message': 'Database connection failed'
            }
        }
    
    @staticmethod
    def _test_database():
        """Test database connectivity"""
        try:
            from models import Contact
            Contact.query.first()
            return True
        except:
            return False
