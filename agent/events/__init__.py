from agent.events.insider import InsiderBuys
from agent.events.sch13d import Schedule13D

LINES = {InsiderBuys.spec.name: InsiderBuys(), Schedule13D.spec.name: Schedule13D()}
