import pytest
from main import bot
from queue import Queue

class mock_Connection:
    def __init__(self, event_queue: Queue):
        self.event_queue = event_queue
        self.orders_placed = []

    def place_new_order(self, contract, order):
        self.orders_placed.append({'symbol': contract.symbol, 'action': order.action})

    def connect_to_IB(self):
        pass

    def request_account_summary(self):
        pass

def test_handling_answers():
    mock_conn = mock_Connection(event_queue=Queue())
    my_bot = bot(connection=mock_conn)
    success_fill_event = {
        'event_type': 'FILL',
        'symbol': 'AAPL',
        'action': 'BUY',
        'quantity': 10,
        'fill_price': 150.0
    }
    fail_fill_event = {
        'event type': 'ERROR', 'reqId': 1, 'code': '123', ''
    }
    my_bot.event_queue.put(success_fill_event)
    my_bot.handle_events()
"""משימה: להחזיר ERROR ולבדוק מה קורה לקוד """