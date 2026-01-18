"""
LUX AI Agent - Automated Email Marketing Assistant
Powered by OpenAI GPT-4o for intelligent email campaign management
"""
import os
import json
import logging
from datetime import datetime, timedelta
from openai import OpenAI
# Import only what we need at module level to avoid circular imports
# Model imports will be done within functions where needed
import base64
import requests
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

class LUXAgent:
    """LUX - Automated Email Marketing AI Agent"""
    
    def __init__(self):
        self._client = None
        self._api_key = os.getenv("OPENAI_API_KEY")
        if not self._api_key:
            logger.warning("OPENAI_API_KEY missing; AI features will be disabled until configured.")
        self.model = "gpt-4o"  # Using GPT-4o for reliable performance
        self.agent_name = "LUX"
        self.agent_personality = """
        You are LUX, an expert email marketing automation agent. You are professional, data-driven, 
        and focused on creating high-converting email campaigns. You understand marketing psychology, 
        audience segmentation, and email best practices. You always aim to maximize engagement rates 
        and conversions while maintaining brand consistency.
        """

    def _get_client(self):
        if self._client:
            return self._client
        if not self._api_key:
            self._api_key = os.getenv("OPENAI_API_KEY")
        if not self._api_key:
            logger.warning("OPENAI_API_KEY missing; skipping OpenAI client initialization.")
            return None
        try:
            self._client = OpenAI(api_key=self._api_key)
            logger.info("LUX AI Agent client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize LUX AI Agent client: {e}")
            self._client = None
        return self._client

    @property
    def client(self):
        return self._get_client()

    def _require_client(self, feature_name: str):
        client = self.client
        if not client:
            logger.warning("OpenAI client unavailable; %s disabled.", feature_name)
            return None
        return client
    
    def generate_campaign_content(self, campaign_objective, target_audience, brand_info=None):
        """Generate email campaign content based on objectives and audience"""
        try:
            prompt = f"""
            As LUX, an email marketing expert, create a high-converting email campaign.
            
            Campaign Objective: {campaign_objective}
            Target Audience: {target_audience}
            Brand Information: {brand_info or 'Professional business'}
            
            Generate a complete email campaign with:
            1. Compelling subject line (under 50 characters)
            2. Professional HTML email content
            3. Clear call-to-action
            4. Personalization elements
            
            Respond in JSON format with:
            {
                "subject": "email subject line",
                "html_content": "complete HTML email content",
                "campaign_name": "descriptive campaign name",
                "recommendations": "optimization tips"
            }
            
            Make the content engaging, professional, and conversion-focused.
            """
            
            client = self._require_client("campaign content generation")
            if not client:
                return None
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.agent_personality},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.7
            )
            
            content = response.choices[0].message.content
            if not content:
                logger.error("LUX received empty response from OpenAI")
                return None
            result = json.loads(content)
            logger.info(f"LUX generated campaign content: {result['campaign_name']}")
            return result
            
        except Exception as e:
            logger.error(f"LUX error generating campaign content: {e}")
            return None
    
    def analyze_audience_segments(self, contacts):
        """Analyze contact data to create audience segments"""
        try:
            # Prepare contact data for analysis
            contact_data = []
            for contact in contacts[:50]:  # Limit for API efficiency
                contact_data.append({
                    'email': contact.email,
                    'company': contact.company or 'Unknown',
                    'tags': contact.tags or '',
                    'created_at': contact.created_at.strftime('%Y-%m-%d') if contact.created_at else ''
                })
            
            prompt = f"""
            As LUX, analyze this contact data and create optimal audience segments for email marketing.
            
            Contact Data: {json.dumps(contact_data[:20])}  # Sample of contacts
            Total Contacts: {len(contacts)}
            
            Create 3-5 audience segments based on:
            - Company types/industries
            - Contact behavior patterns
            - Optimal messaging strategies
            
            Respond in JSON format with:
            {{
                "segments": [
                    {{
                        "name": "segment name",
                        "description": "who this segment includes",
                        "size_estimate": "percentage of audience",
                        "messaging_strategy": "how to communicate with this segment",
                        "recommended_tags": ["tag1", "tag2"]
                    }}
                ],
                "insights": "key findings about the audience"
            }}
            """
            
            client = self._require_client("audience analysis")
            if not client:
                return None
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.agent_personality},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.3
            )
            
            content = response.choices[0].message.content
            if not content:
                logger.error("LUX received empty response from OpenAI for audience analysis")
                return None
            result = json.loads(content)
            logger.info(f"LUX analyzed audience and created {len(result['segments'])} segments")
            return result
            
        except Exception as e:
            logger.error(f"LUX error analyzing audience: {e}")
            return None
    
    def optimize_campaign_performance(self, campaign_id):
        """Analyze campaign performance and provide optimization recommendations"""
        try:
            # Import here to avoid circular imports
            from tracking import get_campaign_analytics
            
            # Get campaign analytics
            analytics = get_campaign_analytics(campaign_id)
            if not analytics:
                return None
            
            campaign_data = {
                'name': analytics['campaign'].name,
                'subject': analytics['campaign'].subject,
                'total_recipients': analytics['total_recipients'],
                'delivery_rate': analytics['delivery_rate'],
                'open_rate': analytics['open_rate'],
                'click_rate': analytics['click_rate'],
                'bounce_rate': analytics['bounce_rate']
            }
            
            prompt = f"""
            As LUX, analyze this email campaign performance and provide optimization recommendations.
            
            Campaign Data: {json.dumps(campaign_data)}
            
            Industry Benchmarks:
            - Average Open Rate: 21.33%
            - Average Click Rate: 2.62%
            - Average Bounce Rate: 0.58%
            
            Provide actionable recommendations to improve performance.
            
            Respond in JSON format with:
            {
                "performance_assessment": "overall performance evaluation",
                "strengths": ["what's working well"],
                "improvements": [
                    {
                        "area": "specific area to improve",
                        "recommendation": "specific action to take",
                        "expected_impact": "predicted improvement"
                    }
                ],
                "next_campaign_suggestions": "ideas for follow-up campaigns"
            }
            """
            
            client = self._require_client("campaign optimization")
            if not client:
                return None
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.agent_personality},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.3
            )
            
            result = json.loads(response.choices[0].message.content)
            logger.info(f"LUX analyzed campaign {campaign_id} performance")
            return result
            
        except Exception as e:
            logger.error(f"LUX error optimizing campaign: {e}")
            return None
    
    def create_automated_campaign(self, campaign_brief, template_id=None):
        """Automatically create and schedule a complete email campaign"""
        try:
            # Import here to avoid circular imports
            from models import Campaign, EmailTemplate, Contact, CampaignRecipient, db
            from email_service import EmailService
            
            # Generate campaign content
            content = self.generate_campaign_content(
                campaign_brief.get('objective', 'Engage audience'),
                campaign_brief.get('target_audience', 'All contacts'),
                campaign_brief.get('brand_info', 'Professional business')
            )
            
            if not content:
                return None
            
            # Create email template if not provided
            if not template_id:
                template = EmailTemplate(
                    name=f"LUX Generated - {content['campaign_name']}",
                    subject=content['subject'],
                    html_content=content['html_content']
                )
                db.session.add(template)
                db.session.flush()
                template_id = template.id
            
            # Create campaign
            campaign = Campaign(
                name=content['campaign_name'],
                subject=content['subject'],
                template_id=template_id,
                status='draft'
            )
            
            # Schedule if requested
            if campaign_brief.get('schedule_time'):
                campaign.scheduled_at = campaign_brief['schedule_time']
                campaign.status = 'scheduled'
            
            db.session.add(campaign)
            db.session.flush()
            
            # Add recipients based on targeting
            contacts_query = Contact.query.filter_by(is_active=True)
            
            # Apply audience filtering if specified
            if campaign_brief.get('target_tags'):
                tags = campaign_brief['target_tags']
                tag_conditions = []
                for tag in tags:
                    tag_conditions.append(Contact.tags.contains(tag))
                if tag_conditions:
                    from sqlalchemy import or_
                    contacts_query = contacts_query.filter(or_(*tag_conditions))
            
            contacts = contacts_query.all()
            
            # Add recipients
            for contact in contacts:
                recipient = CampaignRecipient(
                    campaign_id=campaign.id,
                    contact_id=contact.id
                )
                db.session.add(recipient)
            
            db.session.commit()
            
            result = {
                'campaign_id': campaign.id,
                'campaign_name': campaign.name,
                'recipients_count': len(contacts),
                'recommendations': content.get('recommendations', ''),
                'status': campaign.status
            }
            
            logger.info(f"LUX created automated campaign: {campaign.name} with {len(contacts)} recipients")
            return result
            
        except Exception as e:
            logger.error(f"LUX error creating automated campaign: {e}")
            db.session.rollback()
            return None
    
    def generate_blog_post(self, topic, keywords=None, tone='professional'):
        """Generate SEO-optimized blog post"""
        try:
            keywords_str = ', '.join(keywords) if keywords else ''
            
            prompt = f"""
            Write a comprehensive, SEO-optimized blog post about: {topic}
            
            Keywords to include: {keywords_str}
            Tone: {tone}
            Length: 800-1200 words
            
            Include:
            1. Compelling title
            2. Introduction with hook
            3. Well-structured body with H2/H3 headings
            4. Conclusion with CTA
            5. Natural keyword integration
            
            Return JSON with:
            {{
                "title": "blog post title",
                "content": "full blog content with HTML formatting"
            }}
            """
            
            client = self._require_client("blog post generation")
            if not client:
                return None
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.agent_personality},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.7
            )
            
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            logger.error(f"Blog generation error: {e}")
            return None
    
    def generate_subject_line_variants(self, campaign_objective, original_subject=None):
        """Generate multiple subject line variants for A/B testing"""
        try:
            prompt = f"""
            As LUX, generate 5 high-converting email subject line variants for A/B testing.
            
            Campaign Objective: {campaign_objective}
            Original Subject: {original_subject or 'Not provided'}
            
            Create subject lines that use different psychological triggers:
            - Urgency
            - Curiosity
            - Benefit-focused
            - Personalization
            - Social proof
            
            Respond in JSON format with:
            {
                "variants": [
                    {
                        "subject": "subject line text",
                        "strategy": "psychological trigger used",
                        "predicted_performance": "high/medium/low"
                    }
                ],
                "testing_recommendations": "how to test these effectively"
            }
            
            Keep all subject lines under 50 characters for mobile optimization.
            """
            
            client = self._require_client("subject line variants")
            if not client:
                return None
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.agent_personality},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.8
            )
            
            result = json.loads(response.choices[0].message.content)
            logger.info(f"LUX generated {len(result['variants'])} subject line variants")
            return result
            
        except Exception as e:
            logger.error(f"LUX error generating subject lines: {e}")
            return None
    
    def get_campaign_recommendations(self, campaign_data=None, total_contacts=0):
        """Get AI-powered recommendations for new campaigns based on provided data"""
        try:
            # Accept data as parameters to avoid circular imports
            if campaign_data is None:
                campaign_data = []
            
            prompt = f"""
            As LUX, analyze the current email marketing situation and recommend new campaign strategies.
            
            Current Data:
            - Total Active Contacts: {total_contacts}
            - Recent Campaigns: {json.dumps(campaign_data)}
            - Current Date: {datetime.now().strftime('%Y-%m-%d')}
            
            Provide strategic recommendations for upcoming campaigns considering:
            - Seasonal opportunities
            - Performance trends
            - Audience engagement patterns
            - Industry best practices
            
            Respond in JSON format with:
            {{
                "recommended_campaigns": [
                    {{
                        "campaign_type": "type of campaign",
                        "objective": "primary goal", 
                        "timing": "when to send",
                        "expected_results": "predicted performance",
                        "priority": "high/medium/low"
                    }}
                ],
                "strategic_insights": "key observations and next steps"
            }}
            """
            
            client = self._require_client("campaign recommendations")
            if not client:
                return None
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.agent_personality},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.4
            )
            
            result = json.loads(response.choices[0].message.content)
            logger.info(f"LUX generated {len(result['recommended_campaigns'])} campaign recommendations")
            return result
            
        except Exception as e:
            logger.error(f"LUX error getting recommendations: {e}")
            return None
    
    def generate_email_content(self, prompt, content_type="email_content"):
        """Generate email content using OpenAI"""
        try:
            system_prompt = f"""
            You are LUX, an expert email marketing content generator. Generate compelling {content_type} 
            based on the user's requirements. Always provide 3-5 different options that are:
            - Engaging and professional
            - Action-oriented when appropriate
            - Brand-consistent
            - Optimized for email marketing
            """
            
            client = self._require_client("email content generation")
            if not client:
                return ["Error generating content. Please try again."]
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Generate {content_type} for: {prompt}"}
                ],
                max_tokens=1000,
                temperature=0.8
            )
            
            content = response.choices[0].message.content.strip()
            
            # Split into multiple options
            if "1." in content or "Option 1" in content:
                options = [opt.strip() for opt in content.split('\n') if opt.strip() and any(c.isalnum() for c in opt)]
                return options[:5]
            else:
                # If not formatted as list, create variations
                return [
                    content,
                    content.replace(".", "!"),
                    content + " Act now!",
                    content.replace("your", "our") if "your" in content else content + " Don't miss out!"
                ]
                
        except Exception as e:
            logger.error(f"Error generating email content: {e}")
            return ["Error generating content. Please try again."]
    
    def generate_subject_lines(self, campaign_type, audience=""):
        """Generate email subject line suggestions"""
        try:
            system_prompt = """
            You are LUX, an expert email marketing strategist. Generate compelling email subject lines 
            that maximize open rates. Focus on:
            - Creating urgency and curiosity
            - Keeping under 50 characters when possible
            - Using action words
            - Avoiding spam trigger words
            - Personalizing when appropriate
            """
            
            audience_context = f" for {audience}" if audience else ""
            
            client = self._require_client("subject line generation")
            if not client:
                return ["Error generating subject lines. Please try again."]
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Generate 8 compelling subject lines for a {campaign_type} campaign{audience_context}"}
                ],
                max_tokens=500,
                temperature=0.9
            )
            
            content = response.choices[0].message.content.strip()
            
            # Extract subject lines
            lines = [line.strip() for line in content.split('\n') if line.strip() and len(line.strip()) > 5]
            # Clean up formatting (remove numbers, bullets, etc.)
            cleaned_lines = []
            for line in lines:
                cleaned = line.split('. ', 1)[-1].split('- ', 1)[-1].strip(' "\'')
                if len(cleaned) > 5:
                    cleaned_lines.append(cleaned)
                    
            return cleaned_lines[:8]
            
        except Exception as e:
            logger.error(f"Error generating subject lines: {e}")
            return ["Error generating subject lines. Please try again."]
    
    def generate_campaign_image(self, campaign_description, style="professional marketing"):
        """Generate marketing images using DALL-E"""
        try:
            client = self._require_client("campaign image generation")
            if not client:
                return None
                
            prompt = f"""
            Create a professional marketing image for: {campaign_description}
            
            Style: {style}
            Requirements:
            - High-quality, professional marketing design
            - Suitable for email marketing campaigns
            - Clear, engaging visual that supports the campaign message
            - Modern, clean aesthetic
            - Brand-friendly colors and composition
            """
            
            # Use explicit parameters to avoid any conflicts
            # Note: DALL-E 3 doesn't support 'n' parameter (only generates 1 image)
            response = client.images.generate(
                model="dall-e-3",
                prompt=prompt,
                size="1024x1024",
                quality="standard"
            )
            
            image_url = response.data[0].url
            logger.info(f"LUX generated campaign image: {campaign_description[:50]}...")
            
            return {
                'image_url': image_url,
                'prompt_used': prompt,
                'campaign_description': campaign_description
            }
            
        except Exception as e:
            logger.error(f"LUX error generating image: {e}")
            return None
    
    def fetch_woocommerce_products(self, woocommerce_url, consumer_key, consumer_secret, 
                                  product_limit=10, category_filter=None):
        """Fetch products from WooCommerce API using pure requests - no WooCommerce library"""
        try:
            # Explicitly prevent any WooCommerce library imports
            import sys
            woo_modules = [mod for mod in sys.modules.keys() if 'woocommerce' in mod.lower()]
            if woo_modules:
                logger.warning(f"Detected WooCommerce modules: {woo_modules}. Using requests only.")
            
            # Always use requests library directly to avoid WooCommerce library conflicts
            # Construct API endpoint
            api_url = urljoin(woocommerce_url, '/wp-json/wc/v3/products')
            
            # Set up authentication and parameters
            auth = (consumer_key, consumer_secret)
            params = {
                'per_page': product_limit,
                'status': 'publish',
                'stock_status': 'instock'
            }
            
            if category_filter:
                params['category'] = category_filter
            
            # Use requests directly to avoid any WooCommerce client library issues
            response = requests.get(api_url, auth=auth, params=params, timeout=10)
            
            if response.status_code == 200:
                products = response.json()
                
                # Process products for email use
                processed_products = []
                for product in products:
                    processed_product = {
                        'id': product.get('id'),
                        'name': product.get('name', ''),
                        'price': product.get('price', '0'),
                        'regular_price': product.get('regular_price', '0'),
                        'sale_price': product.get('sale_price', ''),
                        'description': product.get('short_description', ''),
                        'image_url': product.get('images', [{}])[0].get('src', '') if product.get('images') else '',
                        'permalink': product.get('permalink', ''),
                        'categories': [cat.get('name', '') for cat in product.get('categories', [])],
                        'tags': [tag.get('name', '') for tag in product.get('tags', [])],
                        'in_stock': product.get('stock_status') == 'instock',
                        'featured': product.get('featured', False)
                    }
                    processed_products.append(processed_product)
                
                logger.info(f"LUX fetched {len(processed_products)} WooCommerce products")
                return processed_products
            else:
                logger.error(f"WooCommerce API error: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"LUX error fetching WooCommerce products: {e}")
            return None
    
    def create_product_campaign(self, woocommerce_config, campaign_objective, 
                               product_filter=None, include_images=True):
        """Create a product-focused email campaign with WooCommerce integration"""
        try:
            # Ensure no WooCommerce library conflicts by isolating the API call
            logger.info("Starting WooCommerce product campaign creation...")
            
            # Fetch products using isolated approach
            products = self.fetch_woocommerce_products(
                woocommerce_config['url'],
                woocommerce_config['consumer_key'],
                woocommerce_config['consumer_secret'],
                product_limit=woocommerce_config.get('product_limit', 6),
                category_filter=product_filter
            )
            
            if not products:
                return None
            
            # Generate campaign image if requested
            campaign_image = None
            if include_images:
                image_description = f"Product showcase for {campaign_objective} featuring {len(products)} products"
                campaign_image = self.generate_campaign_image(image_description, "e-commerce product showcase")
            
            # Create product-focused campaign content
            prompt = f"""
            As LUX, create a high-converting product email campaign.
            
            Campaign Objective: {campaign_objective}
            Products to Feature: {json.dumps(products[:3])}  # Top 3 products for context
            Total Products Available: {len(products)}
            Campaign Image: {'Available' if campaign_image else 'Not generated'}
            
            Create an HTML email that:
            1. Features the products prominently with images and prices
            2. Includes compelling product descriptions
            3. Has clear call-to-action buttons for each product
            4. Uses professional e-commerce email styling
            5. Includes the campaign image if available
            6. Has a compelling subject line focused on the products
            
            Respond in JSON format with:
            {
                "subject": "product-focused subject line",
                "html_content": "complete HTML email with product showcase",
                "campaign_name": "descriptive campaign name",
                "featured_products": ["list of product names featured"],
                "recommendations": "optimization tips for product campaigns"
            }
            
            Make it conversion-focused with clear pricing and purchase buttons.
            """
            
            client = self._require_client("product campaign generation")
            if not client:
                return None
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.agent_personality},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.7
            )
            
            campaign_content = json.loads(response.choices[0].message.content)
            
            # Add product and image data to response
            result = {
                **campaign_content,
                'products': products,
                'campaign_image': campaign_image,
                'product_count': len(products),
                'woocommerce_integration': True
            }
            
            logger.info(f"LUX created product campaign with {len(products)} products")
            return result
            
        except Exception as e:
            logger.error(f"LUX error creating product campaign: {e}")
            return None

_lux_agent = None


def get_lux_agent():
    """Lazy-load the global LUX agent instance."""
    global _lux_agent
    if _lux_agent is None:
        _lux_agent = LUXAgent()
    return _lux_agent
