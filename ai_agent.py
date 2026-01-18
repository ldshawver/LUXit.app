"""
LUX AI Agent - Automated Email Marketing Assistant
Powered by OpenAI for intelligent email campaign management
"""

import os
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from urllib.parse import urljoin

from openai import OpenAI

# Keep these imports light/consistent.
# NOTE: Anything that touches DB requires Flask app context when executed.
from models import Campaign, Contact, EmailTemplate, CampaignRecipient, db
from email_service import EmailService

logger = logging.getLogger(__name__)


class LUXAgent:
    """LUX - Automated Email Marketing AI Agent"""

    def __init__(self):
        self._client: Optional[OpenAI] = None
        self._api_key: Optional[str] = os.getenv("OPENAI_API_KEY")

        # Use env override if you want to change models without code changes
        self.model: str = os.getenv("OPENAI_MODEL", "gpt-4o")

        self.agent_name = "LUX"
        self.agent_personality = """
You are LUX, an expert email marketing automation agent. You are professional, data-driven,
and focused on creating high-converting email campaigns. You understand marketing psychology,
audience segmentation, and email best practices. You always aim to maximize engagement rates
and conversions while maintaining brand consistency.
""".strip()

        if not self._api_key:
            logger.warning("OPENAI_API_KEY missing; AI features will be disabled until configured.")
        else:
            # Lazy-init client; we can also init immediately, but lazy is safer.
            logger.info("LUX Agent initialized (OpenAI key present).")

    def _get_client(self) -> Optional[OpenAI]:
        if self._client:
            return self._client

        if not self._api_key:
            self._api_key = os.getenv("OPENAI_API_KEY")

        if not self._api_key:
            logger.warning("OPENAI_API_KEY missing; skipping OpenAI client initialization.")
            return None

        try:
            self._client = OpenAI(api_key=self._api_key)
            logger.info("LUX AI Agent OpenAI client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize OpenAI client: {e}")
            self._client = None

        return self._client

    @property
    def client(self) -> Optional[OpenAI]:
        return self._get_client()

    def _require_client(self, feature_name: str) -> Optional[OpenAI]:
        client = self.client
        if not client:
            logger.warning("OpenAI client unavailable; %s disabled.", feature_name)
            return None
        return client

    # ----------------------------
    # Core AI generation helpers
    # ----------------------------

    def generate_campaign_content(
        self,
        campaign_objective: str,
        target_audience: str,
        brand_info: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Generate email campaign content based on objectives and audience."""
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
{{
  "subject": "email subject line",
  "html_content": "complete HTML email content",
  "campaign_name": "descriptive campaign name",
  "recommendations": "optimization tips"
}}
""".strip()

            client = self._require_client("campaign content generation")
            if not client:
                return None

            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.agent_personality},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.7,
            )

            content = response.choices[0].message.content
            if not content:
                logger.error("LUX received empty response from OpenAI")
                return None

            result = json.loads(content)
            logger.info("LUX generated campaign content: %s", result.get("campaign_name"))
            return result

        except Exception as e:
            logger.error("LUX error generating campaign content: %s", e)
            return None

    def analyze_audience_segments(self, contacts: List[Contact]) -> Optional[Dict[str, Any]]:
        """Analyze contact data to create audience segments."""
        try:
            # Prepare contact data for analysis (limit to avoid excessive token usage)
            contact_data: List[Dict[str, Any]] = []
            for contact in contacts[:50]:
                contact_data.append(
                    {
                        "email": getattr(contact, "email", ""),
                        "company": getattr(contact, "company", None) or "Unknown",
                        "tags": getattr(contact, "tags", None) or "",
                        "created_at": contact.created_at.strftime("%Y-%m-%d") if getattr(contact, "created_at", None) else "",
                    }
                )

            prompt = f"""
As LUX, analyze this contact data and create optimal audience segments for email marketing.

Contact Sample: {json.dumps(contact_data[:20])}
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
""".strip()

            client = self._require_client("audience analysis")
            if not client:
                return None

            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.agent_personality},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.3,
            )

            content = response.choices[0].message.content
            if not content:
                logger.error("LUX received empty response from OpenAI for audience analysis")
                return None

            result = json.loads(content)
            logger.info("LUX created %d segments", len(result.get("segments", [])))
            return result

        except Exception as e:
            logger.error("LUX error analyzing audience: %s", e)
            return None

    def optimize_campaign_performance(self, campaign_id: int) -> Optional[Dict[str, Any]]:
        """Analyze campaign performance and provide optimization recommendations."""
        try:
            # Import here to reduce circular-import risk
            from tracking import get_campaign_analytics

            analytics = get_campaign_analytics(campaign_id)
            if not analytics:
                return None

            campaign_data = {
                "name": analytics["campaign"].name,
                "subject": analytics["campaign"].subject,
                "total_recipients": analytics["total_recipients"],
                "delivery_rate": analytics["delivery_rate"],
                "open_rate": analytics["open_rate"],
                "click_rate": analytics["click_rate"],
                "bounce_rate": analytics["bounce_rate"],
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
{{
  "performance_assessment": "overall performance evaluation",
  "strengths": ["what's working well"],
  "improvements": [
    {{
      "area": "specific area to improve",
      "recommendation": "specific action to take",
      "expected_impact": "predicted improvement"
    }}
  ],
  "next_campaign_suggestions": "ideas for follow-up campaigns"
}}
""".strip()

            client = self._require_client("campaign optimization")
            if not client:
                return None

            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.agent_personality},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.3,
            )

            content = response.choices[0].message.content
            if not content:
                logger.error("LUX received empty response from OpenAI for optimization")
                return None

            result = json.loads(content)
            logger.info("LUX analyzed campaign %s performance", campaign_id)
            return result

        except Exception as e:
            logger.error("LUX error optimizing campaign: %s", e)
            return None

    # ----------------------------
    # Campaign creation / automation
    # ----------------------------

    def create_automated_campaign(self, campaign_brief: Dict[str, Any], template_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Automatically create and (optionally) schedule a complete email campaign."""
        try:
            content = self.generate_campaign_content(
                campaign_brief.get("objective", "Engage audience"),
                campaign_brief.get("target_audience", "All contacts"),
                campaign_brief.get("brand_info", "Professional business"),
            )
            if not content:
                return None

            # Create a template if one isn't provided
            if not template_id:
                template = EmailTemplate(
                    name=f"LUX Generated - {content.get('campaign_name', 'Campaign')}",
                    subject=content.get("subject", ""),
                    html_content=content.get("html_content", ""),
                )
                db.session.add(template)
                db.session.flush()
                template_id = template.id

            campaign = Campaign(
                name=content.get("campaign_name", "LUX Campaign"),
                subject=content.get("subject", ""),
                template_id=template_id,
                status="draft",
            )

            if campaign_brief.get("schedule_time"):
                campaign.scheduled_at = campaign_brief["schedule_time"]
                campaign.status = "scheduled"

            db.session.add(campaign)
            db.session.flush()

            # Target active contacts
            contacts_query = Contact.query.filter_by(is_active=True)

            # Tag filtering (OR across tags)
            target_tags = campaign_brief.get("target_tags") or []
            if target_tags:
                from sqlalchemy import or_

                tag_conditions = [Contact.tags.contains(tag) for tag in target_tags]
                contacts_query = contacts_query.filter(or_(*tag_conditions))

            contacts = contacts_query.all()

            for contact in contacts:
                db.session.add(CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id))

            db.session.commit()

            result = {
                "campaign_id": campaign.id,
                "campaign_name": campaign.name,
                "recipients_count": len(contacts),
                "recommendations": content.get("recommendations", ""),
                "status": campaign.status,
            }

            logger.info("LUX created automated campaign: %s (%d recipients)", campaign.name, len(contacts))
            return result

        except Exception as e:
            logger.error("LUX error creating automated campaign: %s", e)
            try:
                db.session.rollback()
            except Exception:
                pass
            return None

    # ----------------------------
    # Content helpers
    # ----------------------------

    def generate_blog_post(self, topic: str, keywords: Optional[List[str]] = None, tone: str = "professional") -> Optional[Dict[str, Any]]:
        """Generate SEO-optimized blog post."""
        try:
            keywords_str = ", ".join(keywords) if keywords else ""
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
""".strip()

            client = self._require_client("blog post generation")
            if not client:
                return None

            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.agent_personality},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.7,
            )

            content = response.choices[0].message.content
            return json.loads(content) if content else None

        except Exception as e:
            logger.error("Blog generation error: %s", e)
            return None

    def generate_subject_line_variants(self, campaign_objective: str, original_subject: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Generate multiple subject line variants for A/B testing."""
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
{{
  "variants": [
    {{
      "subject": "subject line text",
      "strategy": "psychological trigger used",
      "predicted_performance": "high/medium/low"
    }}
  ],
  "testing_recommendations": "how to test these effectively"
}}

Keep all subject lines under 50 characters for mobile optimization.
""".strip()

            client = self._require_client("subject line variants")
            if not client:
                return None

            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.agent_personality},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.8,
            )

            content = response.choices[0].message.content
            if not content:
                return None
            result = json.loads(content)
            logger.info("LUX generated %d subject line variants", len(result.get("variants", [])))
            return result

        except Exception as e:
            logger.error("LUX error generating subject lines: %s", e)
            return None

    def get_campaign_recommendations(self, campaign_data: Optional[List[Dict[str, Any]]] = None, total_contacts: int = 0) -> Optional[Dict[str, Any]]:
        """Get AI-powered recommendations for new campaigns based on provided data."""
        try:
            campaign_data = campaign_data or []

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
""".strip()

            client = self._require_client("campaign recommendations")
            if not client:
                return None

            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.agent_personality},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.4,
            )

            content = response.choices[0].message.content
            if not content:
                return None

            result = json.loads(content)
            logger.info("LUX generated %d campaign recommendations", len(result.get("recommended_campaigns", [])))
            return result

        except Exception as e:
            logger.error("LUX error getting recommendations: %s", e)
            return None

    def generate_email_content(self, prompt: str, content_type: str = "email_content") -> List[str]:
        """Generate email content using OpenAI."""
        try:
            system_prompt = f"""
You are LUX, an expert email marketing content generator. Generate compelling {content_type}
based on the user's requirements. Always provide 3-5 different options that are:
- Engaging and professional
- Action-oriented when appropriate
- Brand-consistent
- Optimized for email marketing
""".strip()

            client = self._require_client("email content generation")
            if not client:
                return ["AI is disabled (missing OPENAI_API_KEY)."]

            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Generate {content_type} for: {prompt}"},
                ],
                max_tokens=1000,
                temperature=0.8,
            )

            content = (response.choices[0].message.content or "").strip()
            if not content:
                return ["No content returned. Please try again."]

            # Try to extract list-like content
            lines = [line.strip() for line in content.split("\n") if line.strip()]
            # If it looks like a list, return cleaned items
            if any(line.startswith(("1.", "2.", "3.", "Option")) for line in lines):
                cleaned = []
                for line in lines:
                    # strip leading numbering/bullets
                    if ". " in line[:4]:
                        line = line.split(". ", 1)[1]
                    cleaned.append(line.strip())
                return cleaned[:5]

            # Otherwise return a few simple variations
            variations = [content]
            variations.append(content.replace(".", "!") if "." in content else content + "!")
            variations.append(content + " Act now!" if "Act now" not in content else content)
            variations.append(content.replace("your", "our") if "your" in content else content + " Don't miss out!")
            return variations[:5]

        except Exception as e:
            logger.error("Error generating email content: %s", e)
            return ["Error generating content. Please try again."]

    def generate_subject_lines(self, campaign_type: str, audience: str = "") -> List[str]:
        """Generate email subject line suggestions."""
        try:
            system_prompt = """
You are LUX, an expert email marketing strategist. Generate compelling email subject lines
that maximize open rates. Focus on:
- Creating urgency and curiosity
- Keeping under 50 characters when possible
- Using action words
- Avoiding spam trigger words
- Personalizing when appropriate
""".strip()

            audience_context = f" for {audience}" if audience else ""

            client = self._require_client("subject line generation")
            if not client:
                return ["AI is disabled (missing OPENAI_API_KEY)."]

            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Generate 8 compelling subject lines for a {campaign_type} campaign{audience_context}"},
                ],
                max_tokens=500,
                temperature=0.9,
            )

            content = (response.choices[0].message.content or "").strip()
            if not content:
                return ["No subject lines returned. Please try again."]

            lines = [line.strip() for line in content.split("\n") if line.strip()]
            cleaned_lines: List[str] = []
            for line in lines:
                cleaned = line
                if ". " in cleaned[:5]:
                    cleaned = cleaned.split(". ", 1)[1]
                if cleaned.startswith("- "):
                    cleaned = cleaned[2:]
                cleaned = cleaned.strip(' "\'')
                if len(cleaned) > 5:
                    cleaned_lines.append(cleaned)

            return cleaned_lines[:8] if cleaned_lines else [content][:1]

        except Exception as e:
            logger.error("Error generating subject lines: %s", e)
            return ["Error generating subject lines. Please try again."]

    # ----------------------------
    # Images (DALL·E 3)
    # ----------------------------

    def generate_campaign_image(self, campaign_description: str, style: str = "professional marketing") -> Optional[Dict[str, Any]]:
        """Generate marketing images using DALL·E."""
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
""".strip()

            # DALL·E 3: do NOT pass n=
            response = client.images.generate(
                model="dall-e-3",
                prompt=prompt,
                size="1024x1024",
                quality="standard",
            )

            image_url = response.data[0].url
            logger.info("LUX generated campaign image: %s...", campaign_description[:50])

            return {
                "image_url": image_url,
                "prompt_used": prompt,
                "campaign_description": campaign_description,
            }

        except Exception as e:
            logger.error("LUX error generating image: %s", e)
            return None

    # ----------------------------
    # WooCommerce (requests only)
    # ----------------------------

    def fetch_woocommerce_products(
        self,
        woocommerce_url: str,
        consumer_key: str,
        consumer_secret: str,
        product_limit: int = 10,
        category_filter: Optional[str] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """Fetch products from WooCommerce API using requests (no WooCommerce library)."""
        try:
            api_url = urljoin(woocommerce_url, "/wp-json/wc/v3/products")

            auth = (consumer_key, consumer_secret)
            params: Dict[str, Any] = {
                "per_page": product_limit,
                "status": "publish",
                "stock_status": "instock",
            }
            if category_filter:
                params["category"] = category_filter

            response = requests.get(api_url, auth=auth, params=params, timeout=10)
            if response.status_code != 200:
                logger.error("WooCommerce API error: %s - %s", response.status_code, response.text)
                return None

            products = response.json()
            processed: List[Dict[str, Any]] = []
            for product in products:
                processed.append(
                    {
                        "id": product.get("id"),
                        "name": product.get("name", ""),
                        "price": product.get("price", "0"),
                        "regular_price": product.get("regular_price", "0"),
                        "sale_price": product.get("sale_price", ""),
                        "description": product.get("short_description", ""),
                        "image_url": product.get("images", [{}])[0].get("src", "") if product.get("images") else "",
                        "permalink": product.get("permalink", ""),
                        "categories": [cat.get("name", "") for cat in product.get("categories", [])],
                        "tags": [tag.get("name", "") for tag in product.get("tags", [])],
                        "in_stock": product.get("stock_status") == "instock",
                        "featured": product.get("featured", False),
                    }
                )

            logger.info("LUX fetched %d WooCommerce products", len(processed))
            return processed

        except Exception as e:
            logger.error("LUX error fetching WooCommerce products: %s", e)
            return None

    def create_product_campaign(
        self,
        woocommerce_config: Dict[str, Any],
        campaign_objective: str,
        product_filter: Optional[str] = None,
        include_images: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Create a product-focused email campaign with WooCommerce integration."""
        try:
            products = self.fetch_woocommerce_products(
                woocommerce_config["url"],
                woocommerce_config["consumer_key"],
                woocommerce_config["consumer_secret"],
                product_limit=int(woocommerce_config.get("product_limit", 6)),
                category_filter=product_filter,
            )
            if not products:
                return None

            campaign_image = None
            if include_images:
                image_description = f"Product showcase for {campaign_objective} featuring {len(products)} products"
                campaign_image = self.generate_campaign_image(image_description, "e-commerce product showcase")

            prompt = f"""
As LUX, create a high-converting product email campaign.

Campaign Objective: {campaign_objective}
Products to Feature (sample): {json.dumps(products[:3])}
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
{{
  "subject": "product-focused subject line",
  "html_content": "complete HTML email with product showcase",
  "campaign_name": "descriptive campaign name",
  "featured_products": ["list of product names featured"],
  "recommendations": "optimization tips for product campaigns"
}}
""".strip()

            client = self._require_client("product campaign generation")
            if not client:
                return None

            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.agent_personality},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.7,
            )

            content = response.choices[0].message.content
            if not content:
                return None

            campaign_content = json.loads(content)
            result = {
                **campaign_content,
                "products": products,
                "campaign_image": campaign_image,
                "product_count": len(products),
                "woocommerce_integration": True,
            }

            logger.info("LUX created product campaign with %d products", len(products))
            return result

        except Exception as e:
            logger.error("LUX error creating product campaign: %s", e)
            return None


_lux_agent: Optional[LUXAgent] = None


def get_lux_agent() -> LUXAgent:
    """Lazy-load the global LUX agent instance."""
    global _lux_agent
    if _lux_agent is None:
        _lux_agent = LUXAgent()
    return _lux_agent
