"""
Populate company secrets from environment variables
Run: python populate_secrets.py
"""
import os
from app import app, db
from models import Company, CompanySecret

# All secrets to populate
SECRETS = {
    'CLICKADILLA_TOKEN': os.getenv('CLICKADILLA_TOKEN'),
    'ENCRYPTION_MASTER_KEY': os.getenv('ENCRYPTION_MASTER_KEY'),
    'EXOCLICK_API_BASE': os.getenv('EXOCLICK_API_BASE'),
    'EXOCLICK_API_TOKEN': os.getenv('EXOCLICK_API_TOKEN'),
    'GA4_PROPERTY_ID': os.getenv('GA4_PROPERTY_ID'),
    'GA4_SERVICE_ACCOUNT_JSON': os.getenv('GA4_SERVICE_ACCOUNT_JSON'),
    'GOOGLE_ADS_CLIENT_ID': os.getenv('GOOGLE_ADS_CLIENT_ID'),
    'GOOGLE_ADS_CLIENT_SECRET': os.getenv('GOOGLE_ADS_CLIENT_SECRET'),
    'GOOGLE_ADS_CUSTOMER_ID': os.getenv('GOOGLE_ADS_CUSTOMER_ID'),
    'GOOGLE_ADS_DEVELOPER_TOKEN': os.getenv('GOOGLE_ADS_DEVELOPER_TOKEN'),
    'GOOGLE_ADS_REFRESH_TOKEN': os.getenv('GOOGLE_ADS_REFRESH_TOKEN'),
    'MS365_CLIENT_ID': os.getenv('MS365_CLIENT_ID'),
    'MS365_CLIENT_SECRET': os.getenv('MS365_CLIENT_SECRET'),
    'MS365_TENANT_ID': os.getenv('MS365_TENANT_ID'),
    'OPENAI_API_KEY': os.getenv('OPENAI_API_KEY'),
    'TUBECORPORATE_CAMPAIGN_ID': os.getenv('TUBECORPORATE_CAMPAIGN_ID'),
    'TUBECORPORATE_DC': os.getenv('TUBECORPORATE_DC'),
    'TUBECORPORATE_MC': os.getenv('TUBECORPORATE_MC'),
    'TUBECORPORATE_PROMO': os.getenv('TUBECORPORATE_PROMO'),
    'TUBECORPORATE_TC': os.getenv('TUBECORPORATE_TC'),
    'TWITTER_API_KEY': os.getenv('TWITTER_API_KEY'),
    'TWITTER_API_SECRET': os.getenv('TWITTER_API_SECRET'),
    'TWITTER_BEARER_TOKEN': os.getenv('TWITTER_BEARER_TOKEN'),
    'TWITTER_CLIENT_ID': os.getenv('TWITTER_CLIENT_ID'),
    'TWITTER_CLIENT_SECRET': os.getenv('TWITTER_CLIENT_SECRET'),
    'WC_CONSUMER_KEY': os.getenv('WC_CONSUMER_KEY'),
    'WC_CONSUMER_SECRET': os.getenv('WC_CONSUMER_SECRET'),
    'WC_STORE_URL': os.getenv('WC_STORE_URL'),
}

def populate_secrets():
    """Populate secrets for Lucifer Cruz company"""
    with app.app_context():
        company = Company.query.filter_by(name='Lucifer Cruz').first()
        if not company:
            print("❌ Company 'Lucifer Cruz' not found")
            return False
        
        count = 0
        for key, value in SECRETS.items():
            if value:  # Only add if value exists
                secret = CompanySecret.query.filter_by(
                    company_id=company.id, key=key
                ).first()
                if secret:
                    secret.value = value
                else:
                    secret = CompanySecret(
                        company_id=company.id,
                        key=key,
                        value=value,
                    )
                    db.session.add(secret)
                print(f"✓ Added {key}")
                count += 1
            else:
                print(f"⊘ Skipped {key} (not in environment)")

        db.session.commit()
        print(f"\n✓ {count} secrets populated for {company.name}")
        return True

if __name__ == '__main__':
    populate_secrets()
