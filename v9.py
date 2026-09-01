import os
import json
import sqlite3
import uvicorn
from fastapi import FastAPI, Request
from mcp.server import Server
from mcp.server.sse import SseServerTransport
import mcp.types as types

# 1. Configuration
DB_PATH = "sample.db"
PORT = 8989

# 2. Initialize the low-level MCP Server
server = Server("sqlite-mcp-server")


# ---------------------------------------------------------
# Helper: Convert SQLite rows to Markdown table
# ---------------------------------------------------------
def rows_to_markdown(cursor, rows):
    if not cursor.description:
        return "Query returned no columns."

    columns = [desc[0] for desc in cursor.description]

    # Header
    lines = []

    lines.append(
        "| " + " | ".join(columns) + " |"
    )

    lines.append(
        "| " + " | ".join(["---"] * len(columns)) + " |"
    )

    # Rows
    for row in rows:
        values = []

        for value in row:
            if value is None:
                value = "NULL"
            else:
                value = str(value)

            # Escape Markdown table characters
            value = value.replace("\\", "\\\\")
            value = value.replace("|", "\\|")
            value = value.replace("\n", " ")

            values.append(value)

        lines.append(
            "| " + " | ".join(values) + " |"
        )

    if not rows:
        lines.append(
            "| " + " | ".join([""] * len(columns)) + " |"
        )

    return "\n".join(lines)


# ---------------------------------------------------------
# Helper: Check whether SQL is read-only
# ---------------------------------------------------------
def validate_read_query(sql):
    sql = sql.strip()

    if not sql:
        return False, "SQL query cannot be empty."

    # Remove a trailing semicolon.
    # A semicolon anywhere else means multiple statements.
    if sql.endswith(";"):
        sql = sql[:-1].rstrip()

    if ";" in sql:
        return False, "Multiple SQL statements are not allowed."

    # SQLite allows comments, so reject SQL comments to make
    # validation easier and less ambiguous.
    if "--" in sql or "/*" in sql or "*/" in sql:
        return False, "SQL comments are not allowed."

    # Only SELECT and WITH queries are allowed.
    #
    # SQLite WITH can be used with INSERT/UPDATE/DELETE,
    # therefore simply checking "WITH" is not sufficient.
    #
    # The authorizer below provides the stronger protection.
    first_word = sql.split(None, 1)[0].upper()

    if first_word not in ("SELECT", "WITH"):
        return False, "Only SELECT or WITH queries are allowed."

    return True, sql


# ---------------------------------------------------------
# Helper: SQLite read-only authorizer
# ---------------------------------------------------------
def readonly_authorizer(action, arg1, arg2, dbname, source):
    """
    SQLite authorizer callback.

    Allow only operations required for reading data.
    """

    allowed = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_TRANSACTION,
    }

    if action in allowed:
        return sqlite3.SQLITE_OK

    return sqlite3.SQLITE_DENY


# 3. Define the tools available to the AI
@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """Tell the AI what tools are available and what parameters they take."""

    return [
        types.Tool(
            name="list_tables",
            description="List all user-created tables in the local SQLite database.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),

        types.Tool(
            name="describe_table",
            description=(
                "Show a table's columns, data types, primary keys, "
                "NOT NULL constraints, default values, foreign keys, "
                "and indexes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "The name of the SQLite table to describe."
                    }
                },
                "required": ["table_name"],
            },
        ),

        types.Tool(
            name="get_first_record",
            description="Get the very first record from a specified local SQLite table.",
            inputSchema={
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "The name of the SQLite table to query."
                    }
                },
                "required": ["table_name"],
            },
        ),

        types.Tool(
            name="insert_row",
            description="Insert a new record into a specified local SQLite table.",
            inputSchema={
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "The name of the SQLite table to insert a record into."
                    },
                    "data": {
                        "type": "object",
                        "description": "A JSON object mapping column names to their values.",
                        "additionalProperties": True
                    }
                },
                "required": ["table_name", "data"],
            },
        ),

        types.Tool(
            name="read_query",
            description=(
                "Run a read-only SQLite SELECT or WITH query and return "
                "the results as a Markdown table. The query cannot modify "
                "the database. The limit parameter controls the maximum "
                "number of rows returned."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": (
                            "A read-only SQLite SELECT or WITH SQL query. "
                            "INSERT, UPDATE, DELETE, DROP, ALTER and other "
                            "modifying statements are not allowed."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of rows to return.",
                        "minimum": 1,
                        "maximum": 1000,
                        "default": 100,
                    },
                },
                "required": ["sql", "limit"],
            },
        ),
    ]


# 4. Handle the execution of the tools
@server.call_tool()
async def handle_call_tool(
    name: str,
    arguments: dict | None
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:

    # ---------------------------------------------------------
    # list_tables
    # ---------------------------------------------------------
    if name == "list_tables":

        if not os.path.exists(DB_PATH):
            return [
                types.TextContent(
                    type="text",
                    text=f"Error: Database file '{DB_PATH}' does not exist locally."
                )
            ]

        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name NOT LIKE 'sqlite_%'
                ORDER BY name
            """)

            tables = [row[0] for row in cursor.fetchall()]

            conn.close()

            if tables:
                result_str = json.dumps(tables, indent=2)
            else:
                result_str = "No user-created tables found."

            return [
                types.TextContent(
                    type="text",
                    text=result_str
                )
            ]

        except sqlite3.Error as e:
            return [
                types.TextContent(
                    type="text",
                    text=f"SQLite Error: {str(e)}"
                )
            ]

        except Exception as e:
            return [
                types.TextContent(
                    type="text",
                    text=f"Unexpected Error: {str(e)}"
                )
            ]


    # ---------------------------------------------------------
    # describe_table
    # ---------------------------------------------------------
    if name == "describe_table":

        if not arguments or "table_name" not in arguments:
            raise ValueError("Missing required argument 'table_name'")

        table_name = arguments["table_name"]

        if not isinstance(table_name, str) or not table_name.isidentifier():
            return [
                types.TextContent(
                    type="text",
                    text="Error: Invalid table name format."
                )
            ]

        if not os.path.exists(DB_PATH):
            return [
                types.TextContent(
                    type="text",
                    text=f"Error: Database file '{DB_PATH}' does not exist locally."
                )
            ]

        conn = None

        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            # Check that the table exists
            cursor.execute("""
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name = ?
            """, (table_name,))

            if cursor.fetchone() is None:
                return [
                    types.TextContent(
                        type="text",
                        text=f"Error: Table '{table_name}' does not exist."
                    )
                ]

            # Columns
            cursor.execute(
                f"PRAGMA table_info({table_name})"
            )

            column_rows = cursor.fetchall()

            columns = []

            for row in column_rows:
                cid, name_col, col_type, notnull, default, pk = row

                columns.append({
                    "name": name_col,
                    "type": col_type,
                    "primary_key": bool(pk),
                    "primary_key_position": pk,
                    "not_null": bool(notnull),
                    "default": default,
                })

            # Foreign keys
            cursor.execute(
                f"PRAGMA foreign_key_list({table_name})"
            )

            foreign_key_rows = cursor.fetchall()

            foreign_keys = []

            for row in foreign_key_rows:
                foreign_keys.append({
                    "id": row[0],
                    "sequence": row[1],
                    "referenced_table": row[2],
                    "column": row[3],
                    "referenced_column": row[4],
                    "on_update": row[5],
                    "on_delete": row[6],
                    "match": row[7],
                })

            # Indexes
            cursor.execute(
                f"PRAGMA index_list({table_name})"
            )

            index_rows = cursor.fetchall()

            indexes = []

            for row in index_rows:
                index_name = row[1]
                unique = row[2]

                origin = row[3] if len(row) > 3 else None
                partial = row[4] if len(row) > 4 else None

                cursor.execute(
                    f"PRAGMA index_info({index_name})"
                )

                index_columns = [
                    index_row[2]
                    for index_row in cursor.fetchall()
                ]

                indexes.append({
                    "name": index_name,
                    "unique": bool(unique),
                    "origin": origin,
                    "partial": bool(partial)
                    if partial is not None else False,
                    "columns": index_columns,
                })

            result = {
                "table": table_name,
                "columns": columns,
                "foreign_keys": foreign_keys,
                "indexes": indexes,
            }

            result_str = json.dumps(
                result,
                indent=2,
                default=str
            )

            return [
                types.TextContent(
                    type="text",
                    text=result_str
                )
            ]

        except sqlite3.Error as e:
            return [
                types.TextContent(
                    type="text",
                    text=f"SQLite Error: {str(e)}"
                )
            ]

        except Exception as e:
            return [
                types.TextContent(
                    type="text",
                    text=f"Unexpected Error: {str(e)}"
                )
            ]

        finally:
            if conn:
                conn.close()


    # ---------------------------------------------------------
    # get_first_record
    # ---------------------------------------------------------
    if name == "get_first_record":

        if not arguments or "table_name" not in arguments:
            raise ValueError("Missing required argument 'table_name'")

        table_name = arguments["table_name"]

        if not isinstance(table_name, str) or not table_name.isidentifier():
            return [
                types.TextContent(
                    type="text",
                    text="Error: Invalid table name format."
                )
            ]

        if not os.path.exists(DB_PATH):
            return [
                types.TextContent(
                    type="text",
                    text=f"Error: Database file '{DB_PATH}' does not exist locally."
                )
            ]

        conn = None

        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute(
                f"SELECT * FROM {table_name} LIMIT 1"
            )

            record = cursor.fetchone()

            if record:
                result_str = json.dumps(
                    dict(record),
                    indent=2
                )
            else:
                result_str = (
                    f"Table '{table_name}' is empty or has no records."
                )

            return [
                types.TextContent(
                    type="text",
                    text=result_str
                )
            ]

        except sqlite3.OperationalError as e:
            return [
                types.TextContent(
                    type="text",
                    text=f"SQLite Error: {str(e)}"
                )
            ]

        except Exception as e:
            return [
                types.TextContent(
                    type="text",
                    text=f"Unexpected Error: {str(e)}"
                )
            ]

        finally:
            if conn:
                conn.close()


    # ---------------------------------------------------------
    # insert_row
    # ---------------------------------------------------------
    if name == "insert_row":
        
        if not arguments or "table_name" not in arguments or "data" not in arguments:
            raise ValueError("Missing required arguments 'table_name' or 'data'")

        table_name = arguments["table_name"]
        data = arguments["data"]

        # Basic Table name validation to avoid SQL Injection
        if not isinstance(table_name, str) or not table_name.isidentifier():
            return [
                types.TextContent(
                    type="text",
                    text="Error: Invalid table name format."
                )
            ]

        if not isinstance(data, dict) or not data:
            return [
                types.TextContent(
                    type="text",
                    text="Error: 'data' must be a non-empty JSON object."
                )
            ]

        # Basic Column validation to avoid SQL injection on keys
        for col in data.keys():
            if not isinstance(col, str) or not col.isidentifier():
                return [
                    types.TextContent(
                        type="text",
                        text=f"Error: Invalid column name format for '{col}'."
                    )
                ]

        if not os.path.exists(DB_PATH):
            return [
                types.TextContent(
                    type="text",
                    text=f"Error: Database file '{DB_PATH}' does not exist locally."
                )
            ]

        conn = None
        
        try:
            # We connect in default read-write mode
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            # Prepare the INSERT query
            columns = list(data.keys())
            values = list(data.values())
            
            columns_str = ", ".join(columns)
            placeholders_str = ", ".join(["?"] * len(columns))
            
            sql = f"INSERT INTO {table_name} ({columns_str}) VALUES ({placeholders_str})"
            
            cursor.execute(sql, values)
            conn.commit()

            last_id = cursor.lastrowid
            
            return [
                types.TextContent(
                    type="text",
                    text=f"Successfully inserted record into '{table_name}'. Last Inserted Row ID: {last_id}"
                )
            ]

        except sqlite3.Error as e:
            if conn:
                conn.rollback()
            return [
                types.TextContent(
                    type="text",
                    text=f"SQLite Error: {str(e)}"
                )
            ]

        except Exception as e:
            if conn:
                conn.rollback()
            return [
                types.TextContent(
                    type="text",
                    text=f"Unexpected Error: {str(e)}"
                )
            ]

        finally:
            if conn:
                conn.close()


    # ---------------------------------------------------------
    # read_query
    # ---------------------------------------------------------
    if name == "read_query":

        if not arguments:
            raise ValueError("Missing arguments.")

        if "sql" not in arguments:
            raise ValueError("Missing required argument 'sql'")

        sql = arguments["sql"]

        if not isinstance(sql, str):
            return [
                types.TextContent(
                    type="text",
                    text="Error: 'sql' must be a string."
                )
            ]

        # Default limit
        limit = arguments.get("limit", 100)

        if not isinstance(limit, int):
            return [
                types.TextContent(
                    type="text",
                    text="Error: 'limit' must be an integer."
                )
            ]

        # Protect against excessive result sets
        if limit < 1 or limit > 1000:
            return [
                types.TextContent(
                    type="text",
                    text="Error: 'limit' must be between 1 and 1000."
                )
            ]

        # Validate query
        valid, validated_sql = validate_read_query(sql)

        if not valid:
            return [
                types.TextContent(
                    type="text",
                    text=f"Error: {validated_sql}"
                )
            ]

        if not os.path.exists(DB_PATH):
            return [
                types.TextContent(
                    type="text",
                    text=f"Error: Database file '{DB_PATH}' does not exist locally."
                )
            ]

        conn = None

        try:
            # -------------------------------------------------
            # Open SQLite in READ-ONLY mode
            # -------------------------------------------------
            db_uri = (
                "file:"
                + os.path.abspath(DB_PATH)
                + "?mode=ro"
            )

            conn = sqlite3.connect(
                db_uri,
                uri=True
            )

            # SQLite authorizer provides another layer of
            # protection against write operations.
            conn.set_authorizer(readonly_authorizer)

            cursor = conn.cursor()

            # -------------------------------------------------
            # Execute query
            #
            # Use a subquery so we can safely apply LIMIT to
            # SELECT/WITH queries without modifying the user's
            # SQL.
            # -------------------------------------------------
            limited_sql = (
                "SELECT * FROM ("
                + validated_sql
                + f") LIMIT {limit}"
            )

            cursor.execute(limited_sql)

            rows = cursor.fetchall()

            # -------------------------------------------------
            # Convert to Markdown
            # -------------------------------------------------
            result_str = rows_to_markdown(
                cursor,
                rows
            )

            # Add row count
            result_str = (
                f"Returned {len(rows)} row(s).\n\n"
                + result_str
            )

            return [
                types.TextContent(
                    type="text",
                    text=result_str
                )
            ]

        except sqlite3.DatabaseError as e:
            return [
                types.TextContent(
                    type="text",
                    text=f"SQLite Error: {str(e)}"
                )
            ]

        except Exception as e:
            return [
                types.TextContent(
                    type="text",
                    text=f"Unexpected Error: {str(e)}"
                )
            ]

        finally:
            if conn:
                conn.close()


    # ---------------------------------------------------------
    # Unknown tool
    # ---------------------------------------------------------
    raise ValueError(f"Unknown tool: {name}")


# 5. Setup FastAPI and SSE Transport for MCP
app = FastAPI(title="SQLite MCP Server")

from starlette.applications import Starlette
from starlette.routing import Route, Mount

# MCP SSE transport
sse = SseServerTransport("/messages/")


async def handle_sse(request):
    async with sse.connect_sse(
        request.scope,
        request.receive,
        request._send
    ) as (read_stream, write_stream):

        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )


# Create Starlette application
app = Starlette(
    debug=True,
    routes=[
        Route("/sse", endpoint=handle_sse, methods=["GET"]),
        Mount("/messages/", app=sse.handle_post_message),
    ],
)


def setup_dummy_db():
    if not os.path.exists(DB_PATH):
        print(f"[*] Creating sample database at '{DB_PATH}'...")

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                name TEXT,
                email TEXT,
                role TEXT
            )
        """)

        cursor.execute("""
            INSERT INTO users (name, email, role)
            VALUES ('Alice', 'alice@example.com', 'Admin')
        """)

        cursor.execute("""
            INSERT INTO users (name, email, role)
            VALUES ('Bob', 'bob@example.com', 'User')
        """)

        conn.commit()
        conn.close()

        print("[*] Sample database and 'users' table created successfully.")


if __name__ == "__main__":
    setup_dummy_db()

    print(f"[*] Starting MCP SQLite Server on port {PORT}...")

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT
    )

