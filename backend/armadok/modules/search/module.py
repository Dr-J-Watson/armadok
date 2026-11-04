"""Declaration of the `search` module: the search endpoint and the index behind it."""

from armadok.core.module import Module, SearchProvider
from armadok.modules.search import index
from armadok.modules.search.routes import router

module = Module(
    name="search",
    requires=("users", "nodes", "relations"),
    routes=(router,),
    search=(SearchProvider("index", index.run),),
    migrations=(index.MIGRATION,),
    tasks=(index.index_loop,),
)
