"""
Event Service - Phase 3
Handles ticketing, check-ins, and attendee management
"""

from datetime import datetime
import random
import string
from extensions import db
from models import Event, EventTicket, TicketPurchase, EventCheckIn
import logging

logger = logging.getLogger(__name__)

class EventService:
    @staticmethod
    def generate_ticket_code():
        """Generate unique ticket code"""
        return ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))
    
    @staticmethod
    def create_ticket_type(event_id, name, price, quantity, description=None):
        """Create ticket type for event"""
        try:
            ticket = EventTicket(
                event_id=event_id,
                name=name,
                price=price,
                quantity_total=quantity,
                description=description
            )
            db.session.add(ticket)
            db.session.commit()
            return ticket
        except Exception as e:
            logger.error(f"Error creating ticket type: {e}")
            db.session.rollback()
            return None
    
    @staticmethod
    def purchase_ticket(ticket_id, contact_id, quantity, payment_method='card'):
        """Process ticket purchase"""
        try:
            ticket = EventTicket.query.get(ticket_id)
            if not ticket or ticket.quantity_available < quantity:
                return None
            
            total_amount = ticket.price * quantity
            ticket_codes = [EventService.generate_ticket_code() for _ in range(quantity)]
            
            purchase = TicketPurchase(
                ticket_id=ticket_id,
                contact_id=contact_id,
                quantity=quantity,
                total_amount=total_amount,
                payment_method=payment_method,
                payment_status='paid',
                ticket_codes=ticket_codes
            )
            
            ticket.quantity_sold += quantity
            
            db.session.add(purchase)
            db.session.commit()
            return purchase
        except Exception as e:
            logger.error(f"Error purchasing ticket: {e}")
            db.session.rollback()
            return None
    
    @staticmethod
    def check_in_attendee(event_id, contact_id, ticket_purchase_id=None, method='manual', staff_name=None):
        """Check in attendee at event"""
        try:
            check_in = EventCheckIn(
                event_id=event_id,
                contact_id=contact_id,
                ticket_purchase_id=ticket_purchase_id,
                check_in_method=method,
                checked_in_by=staff_name
            )
            
            # Mark ticket as checked in
            if ticket_purchase_id:
                purchase = TicketPurchase.query.get(ticket_purchase_id)
                if purchase:
                    purchase.checked_in = True
                    purchase.check_in_time = datetime.utcnow()
            
            db.session.add(check_in)
            db.session.commit()
            return check_in
        except Exception as e:
            logger.error(f"Error checking in attendee: {e}")
            db.session.rollback()
            return None
    
    @staticmethod
    def get_event_stats(event_id):
        """Get event statistics"""
        try:
            event = Event.query.get(event_id)
            if not event:
                return None
            
            total_tickets = db.session.query(db.func.sum(EventTicket.quantity_total))\
                .filter_by(event_id=event_id).scalar() or 0
            tickets_sold = db.session.query(db.func.sum(EventTicket.quantity_sold))\
                .filter_by(event_id=event_id).scalar() or 0
            total_revenue = db.session.query(db.func.sum(TicketPurchase.total_amount))\
                .join(EventTicket).filter(EventTicket.event_id == event_id).scalar() or 0
            checked_in_count = EventCheckIn.query.filter_by(event_id=event_id).count()
            
            return {
                'event': event,
                'total_tickets': total_tickets,
                'tickets_sold': tickets_sold,
                'tickets_available': total_tickets - tickets_sold,
                'total_revenue': total_revenue,
                'checked_in': checked_in_count,
                'check_in_rate': (checked_in_count / tickets_sold * 100) if tickets_sold > 0 else 0
            }
        except Exception as e:
            logger.error(f"Error getting event stats: {e}")
            return None
