"""
Advanced Workflow Builder Service
Visual automation workflows with conditional logic and multi-channel actions
"""

from datetime import datetime, timedelta
from models import db
import logging
import json

logger = logging.getLogger(__name__)


class WorkflowBuilderService:
    """Service for advanced workflow automation"""
    
    NODE_TYPES = {
        'trigger': ['contact_added', 'tag_applied', 'form_submitted', 'link_clicked', 'email_opened', 'purchase_made'],
        'action': ['send_email', 'send_sms', 'post_social', 'add_tag', 'remove_tag', 'update_field', 'assign_score'],
        'logic': ['if_condition', 'wait', 'split_test', 'goal_check'],
        'exit': ['end_workflow', 'goal_achieved', 'unsubscribe']
    }
    
    @staticmethod
    def create_workflow(name, description, trigger_type, user_id):
        """
        Create new workflow with trigger node.
        
        Args:
            name: Workflow name
            description: Description
            trigger_type: Initial trigger event
            user_id: Creator user ID
        
        Returns:
            dict: Created workflow
        """
        try:
            from models import WorkflowAutomation, WorkflowNode
            
            # Create workflow
            workflow = WorkflowAutomation(
                name=name,
                description=description,
                trigger_type=trigger_type,
                user_id=user_id,
                status='draft'
            )
            db.session.add(workflow)
            db.session.flush()  # Get workflow ID
            
            # Create trigger node
            trigger_node = WorkflowNode(
                workflow_id=workflow.id,
                node_type='trigger',
                action_type=trigger_type,
                position_x=100,
                position_y=100,
                config=json.dumps({})
            )
            db.session.add(trigger_node)
            db.session.commit()
            
            return {
                'success': True,
                'workflow_id': workflow.id,
                'name': name
            }
            
        except Exception as e:
            logger.error(f"Error creating workflow: {e}")
            db.session.rollback()
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def add_node(workflow_id, node_type, action_type, position_x, position_y, config=None):
        """
        Add node to workflow.
        
        Args:
            workflow_id: Workflow ID
            node_type: Type of node (trigger, action, logic, exit)
            action_type: Specific action
            position_x: X position on canvas
            position_y: Y position on canvas
            config: Node configuration
        
        Returns:
            dict: Created node
        """
        try:
            from models import WorkflowNode
            
            if config is None:
                config = {}
            
            node = WorkflowNode(
                workflow_id=workflow_id,
                node_type=node_type,
                action_type=action_type,
                position_x=position_x,
                position_y=position_y,
                config=json.dumps(config)
            )
            
            db.session.add(node)
            db.session.commit()
            
            return {
                'success': True,
                'node_id': node.id,
                'node_type': node_type,
                'action_type': action_type
            }
            
        except Exception as e:
            logger.error(f"Error adding node: {e}")
            db.session.rollback()
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def connect_nodes(workflow_id, source_node_id, target_node_id, condition=None):
        """
        Connect two workflow nodes.
        
        Args:
            workflow_id: Workflow ID
            source_node_id: Source node ID
            target_node_id: Target node ID
            condition: Optional condition for connection (for if/then logic)
        
        Returns:
            dict: Created connection
        """
        try:
            from models import WorkflowConnection
            
            connection = WorkflowConnection(
                workflow_id=workflow_id,
                source_node_id=source_node_id,
                target_node_id=target_node_id,
                condition=condition
            )
            
            db.session.add(connection)
            db.session.commit()
            
            return {
                'success': True,
                'connection_id': connection.id
            }
            
        except Exception as e:
            logger.error(f"Error connecting nodes: {e}")
            db.session.rollback()
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def get_workflow_definition(workflow_id):
        """
        Get complete workflow definition with nodes and connections.
        
        Returns:
            dict: Workflow definition
        """
        try:
            from models import WorkflowAutomation, WorkflowNode, WorkflowConnection
            
            workflow = db.session.get(WorkflowAutomation, workflow_id)
            if not workflow:
                return {'success': False, 'error': 'Workflow not found'}
            
            nodes = WorkflowNode.query.filter_by(workflow_id=workflow_id).all()
            connections = WorkflowConnection.query.filter_by(workflow_id=workflow_id).all()
            
            return {
                'success': True,
                'workflow': {
                    'id': workflow.id,
                    'name': workflow.name,
                    'description': workflow.description,
                    'trigger_type': workflow.trigger_type,
                    'status': workflow.status
                },
                'nodes': [{
                    'id': n.id,
                    'type': n.node_type,
                    'action': n.action_type,
                    'x': n.position_x,
                    'y': n.position_y,
                    'config': json.loads(n.config) if n.config else {}
                } for n in nodes],
                'connections': [{
                    'id': c.id,
                    'source': c.source_node_id,
                    'target': c.target_node_id,
                    'condition': c.condition
                } for c in connections]
            }
            
        except Exception as e:
            logger.error(f"Error getting workflow: {e}")
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def execute_workflow(workflow_id, contact_id, trigger_data=None):
        """
        Execute workflow for a contact.
        
        Args:
            workflow_id: Workflow to execute
            contact_id: Contact to process
            trigger_data: Additional trigger data
        
        Returns:
            dict: Execution result
        """
        try:
            from models import WorkflowExecution, WorkflowNode, WorkflowConnection, Contact
            
            contact = db.session.get(Contact, contact_id)
            if not contact:
                return {'success': False, 'error': 'Contact not found'}
            
            # Create execution record
            execution = WorkflowExecution(
                workflow_id=workflow_id,
                contact_id=contact_id,
                status='running',
                current_node_id=None,
                started_at=datetime.utcnow()
            )
            db.session.add(execution)
            db.session.commit()
            
            # Get trigger node
            trigger_node = WorkflowNode.query.filter_by(
                workflow_id=workflow_id,
                node_type='trigger'
            ).first()
            
            if not trigger_node:
                execution.status = 'failed'
                execution.error_message = 'No trigger node found'
                db.session.commit()
                return {'success': False, 'error': 'No trigger node'}
            
            # Start execution from trigger
            result = WorkflowBuilderService._execute_node(
                execution.id,
                trigger_node.id,
                contact,
                trigger_data or {}
            )
            
            return result
            
        except Exception as e:
            logger.error(f"Error executing workflow: {e}")
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def _execute_node(execution_id, node_id, contact, context):
        """Execute a single workflow node."""
        try:
            from models import WorkflowNode, WorkflowConnection, WorkflowExecution
            
            node = db.session.get(WorkflowNode, node_id)
            execution = db.session.get(WorkflowExecution, execution_id)
            
            execution.current_node_id = node_id
            db.session.commit()
            
            # Execute node action
            if node.node_type == 'action':
                result = WorkflowBuilderService._execute_action(node, contact, context)
                if not result['success']:
                    execution.status = 'failed'
                    execution.error_message = result.get('error')
                    db.session.commit()
                    return result
            
            elif node.node_type == 'logic':
                return WorkflowBuilderService._execute_logic(execution_id, node, contact, context)
            
            elif node.node_type == 'exit':
                execution.status = 'completed'
                execution.completed_at = datetime.utcnow()
                db.session.commit()
                return {'success': True, 'status': 'completed'}
            
            # Find next node(s)
            connections = WorkflowConnection.query.filter_by(
                source_node_id=node_id
            ).all()
            
            # Execute next nodes
            for conn in connections:
                WorkflowBuilderService._execute_node(
                    execution_id,
                    conn.target_node_id,
                    contact,
                    context
                )
            
            return {'success': True}
            
        except Exception as e:
            logger.error(f"Error executing node: {e}")
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def _execute_action(node, contact, context):
        """Execute action node (email, SMS, etc)."""
        config = json.loads(node.config) if node.config else {}
        
        try:
            if node.action_type == 'send_email':
                # Send email action
                from services.email_service import EmailService
                template_id = config.get('template_id')
                if template_id:
                    # Would send email here
                    logger.info(f"Sending email to {contact.email}")
                    return {'success': True}
            
            elif node.action_type == 'send_sms':
                # Send SMS action
                from services.sms_service import SMSService
                message = config.get('message')
                if message and contact.phone:
                    # Would send SMS here
                    logger.info(f"Sending SMS to {contact.phone}")
                    return {'success': True}
            
            elif node.action_type == 'add_tag':
                # Add tag to contact
                tag = config.get('tag')
                if tag:
                    # Would add tag here
                    logger.info(f"Adding tag '{tag}' to contact {contact.id}")
                    return {'success': True}
            
            elif node.action_type == 'assign_score':
                # Update lead score
                score_delta = config.get('score', 0)
                contact.lead_score = (contact.lead_score or 0) + score_delta
                db.session.commit()
                return {'success': True}
            
            return {'success': True}
            
        except Exception as e:
            logger.error(f"Error executing action: {e}")
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def _execute_logic(execution_id, node, contact, context):
        """Execute logic node (wait, condition, split test)."""
        config = json.loads(node.config) if node.config else {}
        
        try:
            if node.action_type == 'wait':
                # Schedule next execution after delay
                delay_minutes = config.get('delay_minutes', 60)
                # Would schedule delayed execution here
                logger.info(f"Waiting {delay_minutes} minutes")
                return {'success': True, 'wait': delay_minutes}
            
            elif node.action_type == 'if_condition':
                # Evaluate condition
                field = config.get('field')
                operator = config.get('operator')
                value = config.get('value')
                
                # Evaluate condition
                contact_value = getattr(contact, field, None)
                condition_met = WorkflowBuilderService._evaluate_condition(
                    contact_value, operator, value
                )
                
                # Find appropriate connection
                from models import WorkflowConnection
                connections = WorkflowConnection.query.filter_by(
                    source_node_id=node.id
                ).all()
                
                for conn in connections:
                    if (condition_met and conn.condition == 'true') or \
                       (not condition_met and conn.condition == 'false'):
                        WorkflowBuilderService._execute_node(
                            execution_id,
                            conn.target_node_id,
                            contact,
                            context
                        )
                
                return {'success': True}
            
            return {'success': True}
            
        except Exception as e:
            logger.error(f"Error executing logic: {e}")
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def _evaluate_condition(value, operator, target):
        """Evaluate a condition."""
        if operator == 'equals':
            return str(value) == str(target)
        elif operator == 'not_equals':
            return str(value) != str(target)
        elif operator == 'contains':
            return target in str(value)
        elif operator == 'greater_than':
            return float(value or 0) > float(target)
        elif operator == 'less_than':
            return float(value or 0) < float(target)
        return False
