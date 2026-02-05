from google.cloud import bigquery
from google.oauth2 import service_account
import logging
import json
from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
import mcp.server.stdio
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import Response
from typing import Any, Optional
import uvicorn

# Set up logging to both stdout and file
logger = logging.getLogger('mcp_bigquery_server')
handler_stdout = logging.StreamHandler()
handler_file = logging.FileHandler('/tmp/mcp_bigquery_server.log')

# Set format for both handlers
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler_stdout.setFormatter(formatter)
handler_file.setFormatter(formatter)

# Add both handlers to logger
logger.addHandler(handler_stdout)
logger.addHandler(handler_file)

# Set overall logging level
logger.setLevel(logging.DEBUG)

logger.info("Starting MCP BigQuery Server")

class BigQueryDatabase:
    def __init__(self, project: str, location: str, key_file: Optional[str], credentials_json: Optional[str], datasets_filter: list[str]):
        """Initialize a BigQuery database client"""
        logger.info(f"Initializing BigQuery client for project: {project}, location: {location}, key_file: {key_file}")
        if not project:
            raise ValueError("Project is required")
        if not location:
            raise ValueError("Location is required")

        credentials: service_account.Credentials | None = None
        if credentials_json:
            try:
                credentials_info = json.loads(credentials_json)
                credentials = service_account.Credentials.from_service_account_info(
                    credentials_info,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )
                logger.info("Using credentials from BIGQUERY_CREDENTIALS environment variable")
            except Exception as e:
                logger.error(f"Error loading service account credentials from JSON: {e}")
                raise ValueError(f"Invalid credentials JSON: {e}")
        elif key_file:
            try:
                # Read and parse the key file manually to handle various formats
                with open(key_file, 'r', encoding='utf-8-sig') as f:
                    key_content = f.read()
                # Strip BOM and replace non-breaking spaces with regular spaces
                key_content = key_content.strip().lstrip('\ufeff')
                key_content = key_content.replace('\xa0', ' ')  # Non-breaking space to regular space
                credentials_info = json.loads(key_content)
                credentials = service_account.Credentials.from_service_account_info(
                    credentials_info,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )
                logger.info("Using credentials from key file")
            except Exception as e:
                logger.error(f"Error loading service account credentials: {e}")
                raise ValueError(f"Invalid key file: {e}")

        self.client = bigquery.Client(credentials=credentials, project=project, location=location)
        self.datasets_filter = datasets_filter

    def execute_query(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Execute a SQL query and return results as a list of dictionaries"""
        logger.debug(f"Executing query: {query}")
        try:
            if params:
                job = self.client.query(query, job_config=bigquery.QueryJobConfig(query_parameters=params))
            else:
                job = self.client.query(query)
                
            results = job.result()
            rows = [dict(row.items()) for row in results]
            logger.debug(f"Query returned {len(rows)} rows")
            return rows
        except Exception as e:
            logger.error(f"Database error executing query: {e}")
            raise
    
    def list_tables(self) -> list[str]:
        """List all tables in the BigQuery database"""
        logger.debug("Listing all tables")

        if self.datasets_filter:
            datasets = [self.client.dataset(dataset) for dataset in self.datasets_filter]
        else:
            datasets = list(self.client.list_datasets())

        logger.debug(f"Found {len(datasets)} datasets")

        tables = []
        for dataset in datasets:
            dataset_tables = self.client.list_tables(dataset.dataset_id)
            tables.extend([
                f"{dataset.dataset_id}.{table.table_id}" for table in dataset_tables
            ])

        logger.debug(f"Found {len(tables)} tables")
        return tables

    def describe_table(self, table_name: str) -> list[dict[str, Any]]:
        """Describe a table in the BigQuery database"""
        logger.debug(f"Describing table: {table_name}")

        parts = table_name.split(".")
        if len(parts) != 2 and len(parts) != 3:
            raise ValueError(f"Invalid table name: {table_name}")

        dataset_id = ".".join(parts[:-1])
        table_id = parts[-1]

        query = f"""
            SELECT ddl
            FROM {dataset_id}.INFORMATION_SCHEMA.TABLES
            WHERE table_name = @table_name;
        """
        return self.execute_query(query, params=[
            bigquery.ScalarQueryParameter("table_name", "STRING", table_id),
        ])

def create_server(project: str, location: str, key_file: Optional[str], credentials_json: Optional[str], datasets_filter: list[str]):
    """Create and configure the MCP server with BigQuery tools."""
    logger.info(f"Starting BigQuery MCP Server with project: {project} and location: {location}")

    db = BigQueryDatabase(project, location, key_file, credentials_json, datasets_filter)
    server = Server("bigquery-manager")

    # Register handlers
    logger.debug("Registering handlers")

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        """List available tools"""
        return [
            types.Tool(
                name="execute-query",
                description="Execute a SELECT query on the BigQuery database",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "SELECT SQL query to execute using BigQuery dialect"},
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="list-tables",
                description="List all tables in the BigQuery database",
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            types.Tool(
                name="describe-table",
                description="Get the schema information for a specific table",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "table_name": {"type": "string", "description": "Name of the table to describe (e.g. my_dataset.my_table)"},
                    },
                    "required": ["table_name"],
                },
            ),
        ]

    @server.call_tool()
    async def handle_call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        """Handle tool execution requests"""
        logger.debug(f"Handling tool execution request: {name}")

        try:
            if name == "list-tables":
                results = db.list_tables()
                return [types.TextContent(type="text", text=str(results))]

            elif name == "describe-table":
                if not arguments or "table_name" not in arguments:
                    raise ValueError("Missing table_name argument")
                results = db.describe_table(arguments["table_name"])
                return [types.TextContent(type="text", text=str(results))]

            if name == "execute-query":
                results = db.execute_query(arguments["query"])
                return [types.TextContent(type="text", text=str(results))]

            else:
                raise ValueError(f"Unknown tool: {name}")
        except Exception as e:
            return [types.TextContent(type="text", text=f"Error: {str(e)}")]

    return server


async def main(project: str, location: str, key_file: Optional[str], credentials_json: Optional[str], datasets_filter: list[str], transport: str = "stdio", port: int = 8000):
    """Main entry point supporting both stdio and SSE transports."""
    server = create_server(project, location, key_file, credentials_json, datasets_filter)

    if transport == "sse":
        logger.info(f"Starting SSE server on port {port}")
        sse = SseServerTransport("/messages/")

        async def handle_sse(request):
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                await server.run(
                    streams[0],
                    streams[1],
                    InitializationOptions(
                        server_name="bigquery",
                        server_version="0.3.0",
                        capabilities=server.get_capabilities(
                            notification_options=NotificationOptions(),
                            experimental_capabilities={},
                        ),
                    ),
                )
            return Response()

        async def handle_health(request):
            return Response(content="OK", media_type="text/plain")

        from starlette.routing import Mount

        app = Starlette(
            debug=True,
            routes=[
                Route("/sse", endpoint=handle_sse),
                Route("/health", endpoint=handle_health),
                Mount("/messages", app=sse.handle_post_message),
            ],
        )

        config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
        server_instance = uvicorn.Server(config)
        await server_instance.serve()
    else:
        # Default to stdio transport
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            logger.info("Server running with stdio transport")
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="bigquery",
                    server_version="0.3.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )
