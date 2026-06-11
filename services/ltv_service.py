"""
LTV Service - Lifetime Value, Cohort Analysis, and RFM Segmentation
"""

from datetime import datetime, timedelta
from sqlalchemy import func, extract
from models import db, Contact, ConversionEvent, Campaign, CampaignRecipient
import pandas as pd
from collections import defaultdict


class LTVService:
    """Service for customer lifetime value and segmentation analysis"""
    
    @staticmethod
    def calculate_customer_ltv(contact_id, prediction_months=12):
        """
        Calculate Customer Lifetime Value.
        
        Args:
            contact_id: Contact ID
            prediction_months: Months to predict forward
        
        Returns:
            dict: LTV metrics
        """
        contact = db.session.get(Contact, contact_id)
        if not contact:
            return None
        
        # Get all conversions for this customer
        conversions = ConversionEvent.query.filter_by(
            contact_id=contact_id
        ).order_by(ConversionEvent.occurred_at.asc()).all()
        
        if not conversions:
            return {
                'total_value': 0,
                'avg_order_value': 0,
                'purchase_frequency': 0,
                'customer_lifespan_days': 0,
                'ltv': 0,
                'predicted_ltv': 0
            }
        
        # Calculate metrics
        total_value = sum(c.event_value for c in conversions)
        num_purchases = len(conversions)
        avg_order_value = total_value / num_purchases if num_purchases > 0 else 0
        
        # Calculate customer lifespan
        first_purchase = conversions[0].occurred_at
        last_purchase = conversions[-1].occurred_at
        lifespan_days = (last_purchase - first_purchase).days
        
        if lifespan_days == 0:
            lifespan_days = 1
        
        # Purchase frequency (purchases per month)
        lifespan_months = lifespan_days / 30.0
        purchase_frequency = num_purchases / lifespan_months if lifespan_months > 0 else num_purchases
        
        # Historical LTV
        historical_ltv = total_value
        
        # Predicted LTV (simple model: avg_order_value * purchase_frequency * prediction_months)
        predicted_ltv = avg_order_value * purchase_frequency * prediction_months
        
        return {
            'total_value': total_value,
            'avg_order_value': avg_order_value,
            'purchase_frequency': purchase_frequency,
            'customer_lifespan_days': lifespan_days,
            'num_purchases': num_purchases,
            'ltv': historical_ltv,
            'predicted_ltv': predicted_ltv,
            'first_purchase': first_purchase,
            'last_purchase': last_purchase
        }
    
    @staticmethod
    def calculate_rfm_score(contact_id, reference_date=None):
        """
        Calculate RFM (Recency, Frequency, Monetary) scores.
        
        Returns scores 1-5 for each dimension:
        - Recency: How recently did they purchase?
        - Frequency: How often do they purchase?
        - Monetary: How much do they spend?
        
        Returns:
            dict: RFM scores and segment
        """
        if reference_date is None:
            reference_date = datetime.utcnow()
        
        contact = db.session.get(Contact, contact_id)
        if not contact:
            return None
        
        # Get all conversions
        conversions = ConversionEvent.query.filter_by(
            contact_id=contact_id
        ).order_by(ConversionEvent.occurred_at.desc()).all()
        
        if not conversions:
            return {
                'recency_score': 0,
                'frequency_score': 0,
                'monetary_score': 0,
                'rfm_segment': 'Inactive',
                'recency_days': None,
                'frequency': 0,
                'monetary_value': 0
            }
        
        # Recency: Days since last purchase
        last_purchase = conversions[0].occurred_at
        recency_days = (reference_date - last_purchase).days
        
        # Frequency: Number of purchases
        frequency = len(conversions)
        
        # Monetary: Total spend
        monetary_value = sum(c.event_value for c in conversions)
        
        # Score recency (1-5, where 5 is most recent)
        if recency_days <= 30:
            recency_score = 5
        elif recency_days <= 60:
            recency_score = 4
        elif recency_days <= 90:
            recency_score = 3
        elif recency_days <= 180:
            recency_score = 2
        else:
            recency_score = 1
        
        # Score frequency (1-5, where 5 is most frequent)
        if frequency >= 10:
            frequency_score = 5
        elif frequency >= 7:
            frequency_score = 4
        elif frequency >= 4:
            frequency_score = 3
        elif frequency >= 2:
            frequency_score = 2
        else:
            frequency_score = 1
        
        # Score monetary (1-5, where 5 is highest spend)
        if monetary_value >= 1000:
            monetary_score = 5
        elif monetary_value >= 500:
            monetary_score = 4
        elif monetary_value >= 250:
            monetary_score = 3
        elif monetary_value >= 100:
            monetary_score = 2
        else:
            monetary_score = 1
        
        # Determine RFM segment
        rfm_segment = LTVService._get_rfm_segment(
            recency_score,
            frequency_score,
            monetary_score
        )
        
        return {
            'recency_score': recency_score,
            'frequency_score': frequency_score,
            'monetary_score': monetary_score,
            'rfm_segment': rfm_segment,
            'recency_days': recency_days,
            'frequency': frequency,
            'monetary_value': monetary_value,
            'rfm_string': f"{recency_score}{frequency_score}{monetary_score}"
        }
    
    @staticmethod
    def _get_rfm_segment(r, f, m):
        """Determine customer segment based on RFM scores"""
        avg_score = (r + f + m) / 3
        
        if r >= 4 and f >= 4 and m >= 4:
            return 'Champions'
        elif r >= 3 and f >= 3 and m >= 3:
            return 'Loyal Customers'
        elif r >= 4 and f <= 2:
            return 'New Customers'
        elif r <= 2 and f >= 3 and m >= 3:
            return 'At Risk'
        elif r <= 2 and f >= 4:
            return 'Cant Lose Them'
        elif r >= 4 and m <= 2:
            return 'Promising'
        elif r <= 2 and f <= 2 and m <= 2:
            return 'Lost'
        elif avg_score >= 3:
            return 'Potential Loyalists'
        else:
            return 'Need Attention'
    
    @staticmethod
    def get_all_customers_rfm():
        """
        Calculate RFM for all customers with conversions.
        
        Returns:
            list: RFM data for all customers
        """
        # Get all contacts with conversions
        contact_ids = db.session.query(
            ConversionEvent.contact_id
        ).distinct().all()
        
        rfm_data = []
        
        for (contact_id,) in contact_ids:
            contact = db.session.get(Contact, contact_id)
            if not contact:
                continue
            
            rfm = LTVService.calculate_rfm_score(contact_id)
            if rfm:
                rfm_data.append({
                    'contact_id': contact_id,
                    'email': contact.email,
                    'name': f"{contact.first_name or ''} {contact.last_name or ''}".strip() or contact.email,
                    **rfm
                })
        
        return rfm_data
    
    @staticmethod
    def get_rfm_segment_summary():
        """
        Get summary statistics for each RFM segment.
        
        Returns:
            dict: Segment summaries
        """
        rfm_data = LTVService.get_all_customers_rfm()
        
        segment_summary = defaultdict(lambda: {
            'count': 0,
            'total_revenue': 0,
            'avg_recency': 0,
            'avg_frequency': 0,
            'avg_monetary': 0
        })
        
        for customer in rfm_data:
            segment = customer['rfm_segment']
            segment_summary[segment]['count'] += 1
            segment_summary[segment]['total_revenue'] += customer['monetary_value']
            segment_summary[segment]['avg_recency'] += customer['recency_days']
            segment_summary[segment]['avg_frequency'] += customer['frequency']
            segment_summary[segment]['avg_monetary'] += customer['monetary_value']
        
        # Calculate averages
        for segment in segment_summary:
            count = segment_summary[segment]['count']
            if count > 0:
                segment_summary[segment]['avg_recency'] /= count
                segment_summary[segment]['avg_frequency'] /= count
                segment_summary[segment]['avg_monetary'] /= count
        
        return dict(segment_summary)
    
    @staticmethod
    def cohort_analysis(months_back=12):
        """
        Perform cohort analysis based on first purchase month.
        
        Args:
            months_back: Number of months to analyze
        
        Returns:
            dict: Cohort retention data
        """
        cutoff_date = datetime.utcnow() - timedelta(days=months_back * 30)
        
        # Get all conversions since cutoff
        conversions = ConversionEvent.query.filter(
            ConversionEvent.occurred_at >= cutoff_date
        ).order_by(ConversionEvent.occurred_at.asc()).all()
        
        # Build cohort data
        cohorts = defaultdict(lambda: {
            'first_month': None,
            'customers': set(),
            'retention': defaultdict(set),
            'revenue': defaultdict(float)
        })
        
        customer_first_purchase = {}
        
        for conversion in conversions:
            contact_id = conversion.contact_id
            purchase_date = conversion.occurred_at
            purchase_month = purchase_date.strftime('%Y-%m')
            
            # Track first purchase month
            if contact_id not in customer_first_purchase:
                customer_first_purchase[contact_id] = purchase_month
                cohorts[purchase_month]['first_month'] = purchase_month
                cohorts[purchase_month]['customers'].add(contact_id)
            
            # Track retention and revenue
            first_month = customer_first_purchase[contact_id]
            cohorts[first_month]['retention'][purchase_month].add(contact_id)
            cohorts[first_month]['revenue'][purchase_month] += conversion.event_value
        
        # Format cohort data
        cohort_data = []
        for cohort_month in sorted(cohorts.keys()):
            cohort = cohorts[cohort_month]
            cohort_size = len(cohort['customers'])
            
            retention_data = {}
            for month in sorted(cohort['retention'].keys()):
                retained = len(cohort['retention'][month])
                retention_pct = (retained / cohort_size * 100) if cohort_size > 0 else 0
                revenue = cohort['revenue'][month]
                
                retention_data[month] = {
                    'customers': retained,
                    'retention_pct': retention_pct,
                    'revenue': revenue
                }
            
            cohort_data.append({
                'cohort_month': cohort_month,
                'cohort_size': cohort_size,
                'retention': retention_data
            })
        
        return cohort_data
    
    @staticmethod
    def get_segment_recommendations(segment):
        """
        Get marketing recommendations for a specific RFM segment.
        
        Returns:
            dict: Recommended actions
        """
        recommendations = {
            'Champions': {
                'strategy': 'Reward and retain',
                'actions': [
                    'Offer exclusive early access to new products',
                    'Create VIP loyalty program benefits',
                    'Ask for referrals and reviews',
                    'Send personalized thank you messages'
                ],
                'channels': ['Email', 'SMS', 'Direct Mail'],
                'priority': 'High'
            },
            'Loyal Customers': {
                'strategy': 'Upsell and cross-sell',
                'actions': [
                    'Recommend complementary products',
                    'Offer bundle deals',
                    'Invite to exclusive events',
                    'Provide loyalty rewards'
                ],
                'channels': ['Email', 'Retargeting Ads'],
                'priority': 'High'
            },
            'New Customers': {
                'strategy': 'Build relationship',
                'actions': [
                    'Send welcome series',
                    'Provide onboarding education',
                    'Offer second purchase incentive',
                    'Request feedback on first purchase'
                ],
                'channels': ['Email', 'SMS'],
                'priority': 'Medium'
            },
            'At Risk': {
                'strategy': 'Re-engage immediately',
                'actions': [
                    'Send win-back campaign',
                    'Offer special discount or incentive',
                    'Request feedback on why they left',
                    'Highlight new products/features'
                ],
                'channels': ['Email', 'Retargeting Ads', 'SMS'],
                'priority': 'High'
            },
            'Cant Lose Them': {
                'strategy': 'Win them back',
                'actions': [
                    'Offer significant discount or deal',
                    'Personalized outreach from sales team',
                    'Highlight improvements made',
                    'Provide VIP treatment to return'
                ],
                'channels': ['Email', 'Phone', 'Direct Mail'],
                'priority': 'Critical'
            },
            'Promising': {
                'strategy': 'Convert to loyal',
                'actions': [
                    'Encourage repeat purchase',
                    'Offer loyalty program enrollment',
                    'Provide educational content',
                    'Send targeted product recommendations'
                ],
                'channels': ['Email', 'Social Media'],
                'priority': 'Medium'
            },
            'Lost': {
                'strategy': 'Final attempt or let go',
                'actions': [
                    'Send last chance offer',
                    'Survey why they churned',
                    'Remove from frequent campaigns',
                    'Consider sunset campaign'
                ],
                'channels': ['Email'],
                'priority': 'Low'
            },
            'Potential Loyalists': {
                'strategy': 'Nurture relationship',
                'actions': [
                    'Encourage more frequent purchases',
                    'Offer loyalty rewards',
                    'Send relevant content',
                    'Provide excellent customer service'
                ],
                'channels': ['Email', 'Social Media'],
                'priority': 'Medium'
            },
            'Need Attention': {
                'strategy': 'Re-activate',
                'actions': [
                    'Send re-engagement campaign',
                    'Offer time-limited incentive',
                    'Showcase popular products',
                    'Remind of account benefits'
                ],
                'channels': ['Email', 'Retargeting Ads'],
                'priority': 'Medium'
            }
        }
        
        return recommendations.get(segment, {
            'strategy': 'Evaluate individually',
            'actions': ['Review customer history', 'Create custom plan'],
            'channels': ['Email'],
            'priority': 'Low'
        })
