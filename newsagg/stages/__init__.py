from .collector import Collector
from .processor import Processor
from .publisher import Publisher
from .writer import Writer

STAGES = {"collector": Collector, "processor": Processor, "writer": Writer, "publisher": Publisher}

__all__ = ["STAGES", "Collector", "Processor", "Publisher", "Writer"]
