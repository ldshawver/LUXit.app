"""
Base AI Agent Class
Foundation for all LUX Marketing AI Agents
"""
import os
import logging
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from openai import OpenAI

logger = logging.getLogger(__name__)


class BaseAgent:
    """Base class for all AI marketing agents"""
    
    def __init__(self, agent_name: str, agent_type: str, description: str = ""):
        """
        Initialize base agent
        
        Args:
            agent_name: Human-readable name of the agent
            agent_type: Technical identifier (e.g., 'brand_strategy', 'content_seo')
            description: Brief description of agent's purpose
        """
        self.agent_name = agent_name
        self.agent_type = agent_type
        self.description = description
        
        self._api_key = os.getenv("OPENAI_API_KEY")
        self._client = None
        if not self._api_key:
            logger.warning(
                "%s: OPENAI_API_KEY missing; AI features will remain disabled until configured.",
                agent_name,
            )
        self.model = "gpt-4o"
        
        self.personality = self._define_personality()
        
        logger.info(f"{self.agent_name} initialized successfully")

    def _get_client(self):
        if self._client:
            return self._client
        if not self._api_key:
            self._api_key = os.getenv("OPENAI_API_KEY")
        if not self._api_key:
            logger.warning("%s: OpenAI client unavailable; missing OPENAI_API_KEY.", self.agent_name)
            return None
        try:
            self._client = OpenAI(api_key=self._api_key)
        except Exception as e:
            logger.error("%s: Failed to initialize OpenAI client: %s", self.agent_name, e)
            self._client = None
        return self._client
    
    def _define_personality(self) -> str:
        """Define the agent's personality and expertise. Override in subclasses."""
        return f"""
        You are {self.agent_name}, an expert AI agent specialized in marketing automation.
        You are professional, data-driven, and focused on delivering actionable results.
        You understand marketing best practices and business objectives.
        """
    
    def generate_with_ai(self, prompt: str, system_prompt: Optional[str] = None, 
                        response_format: Optional[Dict] = None, temperature: float = 0.7) -> Optional[Dict]:
        """
        Generate content using OpenAI GPT-4
        
        Args:
            prompt: User prompt for generation
            system_prompt: Optional system prompt (uses personality if not provided)
            response_format: Optional response format (e.g., {"type": "json_object"})
            temperature: Creativity level (0.0 to 1.0)
            
        Returns:
            Generated content as dict or None on error
        """
        try:
            client = self._get_client()
            if not client:
                return None
            messages = [
                {"role": "system", "content": system_prompt or self.personality},
                {"role": "user", "content": prompt}
            ]
            
            kwargs = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature
            }
            
            if response_format:
                kwargs["response_format"] = response_format
            
            response = client.chat.completions.create(**kwargs)
            
            content = response.choices[0].message.content
            if not content:
                logger.error(f"{self.agent_name}: Received empty response from OpenAI")
                return None
            
            if response_format and response_format.get("type") == "json_object":
                return json.loads(content)
            
            return {"content": content}
            
        except Exception as e:
            logger.error(f"{self.agent_name} AI generation error: {e}")
            return None
    
    def generate_response(self, prompt: str, as_json: bool = True) -> Dict[str, Any]:
        """
        Generate AI response (helper method for agents)
        
        Args:
            prompt: Prompt for AI generation
            as_json: Whether to expect JSON response
            
        Returns:
            Generated response as dict
        """
        response_format = {"type": "json_object"} if as_json else None
        result = self.generate_with_ai(prompt, response_format=response_format)
        return result if result else {}
    
    def generate_image(self, description: str, style: str = "professional marketing") -> Optional[Dict]:
        """
        Generate images using DALL-E 3
        
        Args:
            description: What to generate
            style: Visual style preference
            
        Returns:
            Dict with image_url and metadata or None
        """
        try:
            prompt = f"""
            Create a professional marketing image for: {description}
            
            Style: {style}
            Requirements:
            - High-quality, professional design
            - Suitable for marketing campaigns
            - Clear, engaging visual
            - Modern, clean aesthetic
            """
            
            client = self._get_client()
            if not client:
                return None
            response = client.images.generate(
                model="dall-e-3",
                prompt=prompt,
                size="1024x1024",
                quality="standard"
            )
            
            image_url = response.data[0].url
            
            return {
                'image_url': image_url,
                'prompt_used': prompt,
                'description': description,
                'generated_at': datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"{self.agent_name} image generation error: {e}")
            return None
    
    def log_activity(self, activity_type: str, details: Dict[str, Any], 
                     status: str = "success") -> None:
        """
        Log agent activity to database
        
        Args:
            activity_type: Type of activity performed
            details: Activity details as dict
            status: Status (success, error, warning)
        """
        try:
            from models import AgentLog, db
            
            log_entry = AgentLog(
                agent_type=self.agent_type,
                agent_name=self.agent_name,
                activity_type=activity_type,
                details=json.dumps(details),
                status=status,
                created_at=datetime.now()
            )
            
            db.session.add(log_entry)
            db.session.commit()
            
        except Exception as e:
            logger.error(f"{self.agent_name} logging error: {e}")
    
    def create_task(self, task_name: str, task_data: Dict[str, Any], 
                   scheduled_at: Optional[datetime] = None) -> Optional[int]:
        """
        Create a task for this agent
        
        Args:
            task_name: Name/description of task
            task_data: Task configuration and parameters
            scheduled_at: When to execute (None = now)
            
        Returns:
            Task ID or None on error
        """
        try:
            from models import AgentTask, db
            
            task = AgentTask(
                agent_type=self.agent_type,
                agent_name=self.agent_name,
                task_name=task_name,
                task_data=json.dumps(task_data),
                status='pending',
                scheduled_at=scheduled_at or datetime.now(),
                created_at=datetime.now()
            )
            
            db.session.add(task)
            db.session.commit()
            
            return task.id
            
        except Exception as e:
            logger.error(f"{self.agent_name} task creation error: {e}")
            return None
    
    def get_pending_tasks(self) -> List:
        """Get all pending tasks for this agent"""
        try:
            from models import AgentTask
            
            tasks = AgentTask.query.filter_by(
                agent_type=self.agent_type,
                status='pending'
            ).filter(
                AgentTask.scheduled_at <= datetime.now()
            ).all()
            
            return tasks
            
        except Exception as e:
            logger.error(f"{self.agent_name} error fetching tasks: {e}")
            return []
    
    def complete_task(self, task_id: int, result: Dict[str, Any], 
                     status: str = "completed") -> bool:
        """
        Mark a task as completed with results
        
        Args:
            task_id: Task ID to complete
            result: Task execution result
            status: Final status (completed, failed)
            
        Returns:
            Success status
        """
        try:
            from models import AgentTask, db
            
            task = AgentTask.query.get(task_id)
            if not task:
                return False
            
            task.status = status
            task.result = json.dumps(result)
            task.completed_at = datetime.now()
            
            db.session.commit()
            
            self.log_activity(
                activity_type='task_completion',
                details={'task_id': task_id, 'task_name': task.task_name, 'result': result},
                status=status
            )
            
            return True
            
        except Exception as e:
            logger.error(f"{self.agent_name} error completing task: {e}")
            return False
    
    def submit_for_approval(
        self,
        content_type: str,
        title: str,
        content: Dict[str, Any],
        target_platform: Optional[str] = None,
        confidence_score: Optional[float] = None,
        rationale: Optional[str] = None,
        scheduled_at: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        Submit generated content to approval queue instead of publishing directly.
        ALL AI-generated content must go through this method.
        
        Args:
            content_type: Type of content (social_post, email_campaign, sms_campaign, blog_post, ad_campaign)
            title: Display title for the approval queue
            content: Full content payload
            target_platform: Target platform (instagram, facebook, email, sms, etc.)
            confidence_score: AI confidence level (0.0-1.0)
            rationale: Explanation of why this content was generated
            scheduled_at: Optional scheduled publish time
            
        Returns:
            Approval queue submission result
        """
        try:
            from services.approval_service import ApprovalService, FeatureToggleService
            from models import Company
            
            company = Company.query.first()
            company_id = company.id if company else 1
            
            feature_key = f'agent_{self.agent_type}'
            if not FeatureToggleService.is_enabled(company_id, feature_key):
                logger.info(f"{self.agent_name}: Agent is disabled, skipping content submission")
                return {'success': False, 'reason': 'Agent is disabled'}
            
            if not FeatureToggleService.is_automation_allowed(company_id, feature_key):
                logger.info(f"{self.agent_name}: Automated creation not allowed")
                return {'success': False, 'reason': 'Automated creation not allowed'}
            
            result = ApprovalService.submit_for_approval(
                company_id=company_id,
                content_type=content_type,
                content_id=None,
                title=title,
                content_full=content,
                creation_mode='automated',
                created_by_agent=self.agent_type,
                ai_rationale=rationale or f"Generated by {self.agent_name}",
                confidence_score=confidence_score,
                target_platform=target_platform,
                scheduled_publish_at=scheduled_at
            )
            
            if result.get('success'):
                logger.info(f"{self.agent_name}: Content submitted for approval (ID: {result.get('approval_id')})")
                self.log_activity(
                    activity_type='content_submitted_for_approval',
                    details={
                        'approval_id': result.get('approval_id'),
                        'content_type': content_type,
                        'title': title,
                        'confidence': confidence_score
                    }
                )
            
            return result
            
        except Exception as e:
            logger.error(f"{self.agent_name} error submitting for approval: {e}")
            return {'success': False, 'error': str(e)}
    
    def check_feature_enabled(self, feature_key: Optional[str] = None) -> bool:
        """Check if this agent's feature is enabled"""
        try:
            from services.approval_service import FeatureToggleService
            from models import Company
            
            company = Company.query.first()
            company_id = company.id if company else 1
            
            key = feature_key or f'agent_{self.agent_type}'
            return FeatureToggleService.is_enabled(company_id, key)
            
        except Exception as e:
            logger.error(f"Error checking feature toggle: {e}")
            return False
    
    def execute(self, task_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute agent's main function. Override in subclasses.
        
        Args:
            task_data: Task parameters and configuration
            
        Returns:
            Execution result
        """
        raise NotImplementedError(f"{self.agent_name} must implement execute method")
    
    def run_scheduled_tasks(self) -> int:
        """
        Execute all pending scheduled tasks for this agent
        
        Returns:
            Number of tasks processed
        """
        tasks = self.get_pending_tasks()
        processed = 0
        
        for task in tasks:
            try:
                task_data = json.loads(task.task_data)
                result = self.execute(task_data)
                
                self.complete_task(
                    task.id,
                    result,
                    status='completed' if result.get('success') else 'failed'
                )
                
                processed += 1
                
            except Exception as e:
                logger.error(f"{self.agent_name} task execution error: {e}")
                self.complete_task(
                    task.id,
                    {'success': False, 'error': str(e)},
                    status='failed'
                )
        
        return processed
