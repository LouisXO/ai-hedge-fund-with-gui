from agent.events.insider import InsiderBuys
from agent.events.pead import PeadSmall
from agent.events.sch13d import Schedule13D

LINES = {InsiderBuys.spec.name: InsiderBuys(), Schedule13D.spec.name: Schedule13D(),
         PeadSmall.spec.name: PeadSmall(), "pead_small_sue1": PeadSmall(sue_min=1.0)}
