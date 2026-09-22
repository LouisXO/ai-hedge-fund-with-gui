from agent.events.insider import InsiderBuys
from agent.events.move_news import MoveNews
from agent.events.pead import PeadSmall
from agent.events.sch13d import Schedule13D

LINES = {InsiderBuys.spec.name: InsiderBuys(), Schedule13D.spec.name: Schedule13D(),
         PeadSmall.spec.name: PeadSmall(), "pead_small_sue1": PeadSmall(sue_min=1.0),
         "move_nonews_down": MoveNews("nonews_down"), "move_news_up": MoveNews("news_up"),
         "move_nonews_up": MoveNews("nonews_up"), "move_news_down": MoveNews("news_down")}
