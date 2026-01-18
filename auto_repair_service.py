"""
Automated Error Repair and Testing System
Finds errors, fixes them, tests resolution, and clears error logs
"""
import logging
import json
from datetime import datetime
from extensions import db
from error_logger import ErrorLog, ApplicationDiagnostics
import requests
import subprocess

logger = logging.getLogger(__name__)

class AutoRepairService:
    """Automatically repair errors and verify fixes"""
    
    @staticmethod
    def get_unresolved_errors(limit=10):
        """Get unresolved errors to repair"""
        try:
            errors = ErrorLog.query.filter_by(is_resolved=False).order_by(
                ErrorLog.created_at.desc()
            ).limit(limit).all()
            return [error.to_dict() for error in errors]
        except Exception as e:
            logger.error(f"Error fetching unresolved errors: {e}")
            return []
    
    @staticmethod
    def test_endpoint(endpoint, method='GET', data=None):
        """Test if an endpoint is working"""
        try:
            base_url = "http://localhost:5000"
            url = f"{base_url}{endpoint}"
            
            if method == 'GET':
                response = requests.get(url, timeout=5)
            elif method == 'POST':
                response = requests.post(url, json=data or {}, timeout=5)
            else:
                return {'success': False, 'status_code': None, 'message': 'Unknown method'}
            
            return {
                'success': response.status_code < 400,
                'status_code': response.status_code,
                'message': f'{method} {endpoint}: {response.status_code}'
            }
        except requests.exceptions.Timeout:
            return {'success': False, 'status_code': 'timeout', 'message': f'Timeout testing {endpoint}'}
        except Exception as e:
            return {'success': False, 'status_code': None, 'message': str(e)}
    
    @staticmethod
    def verify_error_resolution(error):
        """Verify if an error has been resolved by testing"""
        error_type = error.get('type', '')
        endpoint = error.get('endpoint', '')
        
        verification_results = {}
        
        # Test based on error type
        if 'ChatbotError' in error_type or 'OpenAI' in error_type:
            # Test chatbot endpoint
            result = AutoRepairService.test_endpoint('/chatbot/send', 'POST', {'message': 'test'})
            verification_results['chatbot_test'] = result
            return result['success']
        
        elif 'NotFound' in error_type and endpoint:
            # Test if endpoint is now accessible
            result = AutoRepairService.test_endpoint(endpoint, 'GET')
            verification_results['endpoint_test'] = result
            return result['success'] and result['status_code'] != 404
        
        elif 'InternalServerError' in error_type and endpoint:
            # Test if endpoint no longer returns 500
            result = AutoRepairService.test_endpoint(endpoint, 'GET')
            verification_results['endpoint_test'] = result
            return result['success'] and result['status_code'] != 500
        
        elif 'DatabaseError' in error_type or 'SQLAlchemy' in error_type:
            # Try a simple database query
            try:
                from models import Contact
                Contact.query.first()
                return True
            except:
                return False
        
        elif 'ConfigurationError' in error_type:
            # Check if configuration was fixed
            import os
            if 'OPENAI_API_KEY' in error.get('message', ''):
                return bool(os.getenv('OPENAI_API_KEY'))
            return False
        
        return False
    
    @staticmethod
    def generate_fix_plan_with_ai(error, client):
        """Use AI to generate a fix plan"""
        try:
            prompt = f"""You are an automated error recovery system. Analyze this error and provide a specific fix:

Error Type: {error.get('type', 'Unknown')}
Error Message: {error.get('message', 'No message')}
Endpoint: {error.get('endpoint', 'Unknown')}
Method: {error.get('method', 'Unknown')}
Time: {error.get('created_at', 'Unknown')}

Provide ONLY a JSON object with:
{{
  "diagnosis": "Brief explanation of what went wrong",
  "root_cause": "The underlying cause",
  "fix_steps": ["step 1", "step 2", "step 3"],
  "test_command": "How to verify the fix (endpoint or command)",
  "severity": "low|medium|high",
  "auto_fixable": true/false
}}

Focus on immediate, actionable fixes."""
            
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": "You are an automated error recovery expert. Respond with ONLY valid JSON, no other text."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=500
            )
            
            response_text = response.choices[0].message.content
            
            # Try to extract JSON
            try:
                # Find JSON in response
                import re
                json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
            except:
                pass
            
            return {
                "diagnosis": "Unable to generate plan",
                "fix_steps": [],
                "auto_fixable": False
            }
        except Exception as e:
            logger.error(f"Error generating fix plan: {e}")
            return {"diagnosis": str(e), "fix_steps": [], "auto_fixable": False}
    
    @staticmethod
    def execute_auto_repair(error_id=None):
        """Execute intelligent auto-repair with proactive monitoring"""
        import os
        from openai import OpenAI
        
        results = {
            'total_errors': 0,
            'tested': 0,
            'resolved': 0,
            'unresolved': 0,
            'details': [],
            'proactive_fixes': []
        }
        
        try:
            # Get API key
            api_key = os.environ.get('OPENAI_API_KEY') or os.getenv('OPENAI_API_KEY')
            if not api_key:
                results['error'] = 'OpenAI API key not configured'
                return results
            
            client = OpenAI(api_key=api_key)
            
            # Get errors to process
            if error_id:
                errors_to_process = [ErrorLog.query.get(error_id)]
                if not errors_to_process[0]:
                    results['error'] = f'Error {error_id} not found'
                    return results
                errors_to_process = [e.to_dict() for e in errors_to_process if e]
            else:
                errors_to_process = AutoRepairService.get_unresolved_errors(limit=10)
            
            results['total_errors'] = len(errors_to_process)
            
            # Proactive checks
            from ai_code_fixer import AICodeFixer
            
            # 1. Check for 404 errors
            route_check = AICodeFixer.auto_fix_404_errors()
            if route_check.get('action_needed'):
                results['proactive_fixes'].append({
                    'type': '404_detection',
                    'missing_routes': route_check.get('missing_routes', [])
                })
            
            # 2. Process each error with intelligence
            for error in errors_to_process:
                try:
                    detail = {
                        'error_id': error['id'],
                        'type': error['type'],
                        'endpoint': error['endpoint'],
                        'status': 'processing'
                    }
                    
                    # Generate intelligent fix plan
                    fix_plan = AutoRepairService.generate_fix_plan_with_ai(error, client)
                    detail['fix_plan'] = fix_plan
                    
                    # Attempt automatic fix if flagged as auto-fixable
                    if fix_plan.get('auto_fixable'):
                        fix_result = AICodeFixer.generate_and_apply_fix(error['type'], error)
                        detail['auto_fix_attempted'] = True
                        detail['fix_result'] = fix_result
                    
                    # Test resolution
                    is_resolved = AutoRepairService.verify_error_resolution(error)
                    detail['resolution_tested'] = True
                    
                    results['tested'] += 1
                    
                    if is_resolved:
                        # Mark as resolved
                        error_obj = ErrorLog.query.get(error['id'])
                        if error_obj:
                            error_obj.is_resolved = True
                            error_obj.resolution_notes = json.dumps({
                                'auto_repaired': True,
                                'repaired_at': datetime.utcnow().isoformat(),
                                'fix_plan': fix_plan,
                                'test_result': 'resolved',
                                'autonomous': True
                            })
                            db.session.commit()
                            detail['status'] = 'resolved'
                            results['resolved'] += 1
                    else:
                        detail['status'] = 'unresolved'
                        detail['suggestion'] = fix_plan.get('diagnosis', 'Manual intervention required')
                        results['unresolved'] += 1
                    
                    results['details'].append(detail)
                
                except Exception as e:
                    logger.error(f"Error processing error {error.get('id')}: {e}")
                    results['details'].append({
                        'error_id': error.get('id'),
                        'status': 'error',
                        'message': str(e)
                    })
            
            logger.info(f"Auto-repair completed: {results['resolved']}/{results['tested']} resolved, {len(results['proactive_fixes'])} proactive fixes")
            return results
        
        except Exception as e:
            logger.error(f"Auto-repair service error: {e}")
            results['error'] = str(e)
            return results
    
    @staticmethod
    def continuous_monitoring():
        """Continuously monitor for errors and auto-fix them"""
        try:
            from error_logger import ApplicationDiagnostics
            
            # Check system health
            health = ApplicationDiagnostics.get_system_health()
            
            # If status is not healthy, trigger auto-repair
            if health.get('status') in ['warning', 'critical']:
                logger.info("Continuous monitoring detected issues - triggering auto-repair")
                return AutoRepairService.execute_auto_repair()
            
            return {'status': 'healthy', 'monitoring': 'active'}
            
        except Exception as e:
            logger.error(f"Continuous monitoring error: {e}")
            return {'error': str(e)}
    
    @staticmethod
    def clear_resolved_errors(older_than_hours=24):
        """Clear resolved errors older than specified hours"""
        try:
            from datetime import timedelta
            
            cutoff_time = datetime.utcnow() - timedelta(hours=older_than_hours)
            cleared = ErrorLog.query.filter(
                ErrorLog.is_resolved == True,
                ErrorLog.created_at < cutoff_time
            ).delete()
            
            db.session.commit()
            logger.info(f"Cleared {cleared} resolved errors older than {older_than_hours} hours")
            return {'cleared': cleared, 'cutoff': cutoff_time.isoformat()}
        except Exception as e:
            logger.error(f"Error clearing resolved errors: {e}")
            return {'error': str(e), 'cleared': 0}
