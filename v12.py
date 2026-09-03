import os
import json
import sqlite3
from typing import Annotated
from pydantic import Field
#from mcp.server.fastmcp import FastMCP

from mcp.server.mcpserver import MCPServer

# 1. Configuration
DB_PATH = "sample.db"
PORT = 8989

# 2. Initialize FastMCP Server (MCP v2 API)
#mcp = MCPServer("sqlite-mcp-server", host="0.0.0.0", port=PORT)
mcp = MCPServer("sqlite-mcp-server")


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


# ---------------------------------------------------------
# 3. Define the tools available to the AI using FastMCP
# ---------------------------------------------------------
@mcp.tool(
    name="list_tables",
    description="List all user-created tables in the local SQLite database."
)
def list_tables() -> str:
    """List all user-created tables in the local SQLite database."""
    if not os.path.exists(DB_PATH):
        return f"Error: Database file '{DB_PATH}' does not exist locally."

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

        return result_str

    except sqlite3.Error as e:
        return f"SQLite Error: {str(e)}"

    except Exception as e:
        return f"Unexpected Error: {str(e)}"


@mcp.tool(
    name="describe_table",
    description=(
        "Show a table's columns, data types, primary keys, "
        "NOT NULL constraints, default values, foreign keys, "
        "and indexes."
    )
)
def describe_table(
    table_name: Annotated[str, Field(description="The name of the SQLite table to describe.")]
) -> str:
    """Show a table's columns, data types, primary keys, NOT NULL constraints, default values, foreign keys, and indexes."""
    if not isinstance(table_name, str) or not table_name.isidentifier():
        return "Error: Invalid table name format."

    if not os.path.exists(DB_PATH):
        return f"Error: Database file '{DB_PATH}' does not exist locally."

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
            return f"Error: Table '{table_name}' does not exist."

        # Columns
        cursor.execute(f"PRAGMA table_info({table_name})")

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
        cursor.execute(f"PRAGMA foreign_key_list({table_name})")

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
        cursor.execute(f"PRAGMA index_list({table_name})")

        index_rows = cursor.fetchall()

        indexes = []

        for row in index_rows:
            index_name = row[1]
            unique = row[2]

            origin = row[3] if len(row) > 3 else None
            partial = row[4] if len(row) > 4 else None

            cursor.execute(f"PRAGMA index_info({index_name})")

            index_columns = [
                index_row[2]
                for index_row in cursor.fetchall()
            ]

            indexes.append({
                "name": index_name,
                "unique": bool(unique),
                "origin": origin,
                "partial": bool(partial) if partial is not None else False,
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

        return result_str

    except sqlite3.Error as e:
        return f"SQLite Error: {str(e)}"

    except Exception as e:
        return f"Unexpected Error: {str(e)}"

    finally:
        if conn:
            conn.close()


@mcp.tool(
    name="create_table",
    description="Create a new table in the local SQLite database with specified columns and data types."
)
def create_table(
    table_name: Annotated[str, Field(description="The name of the SQLite table to create.")],
    columns: Annotated[dict, Field(description="A JSON object mapping column names to their SQLite data types and constraints (e.g., {'id': 'INTEGER PRIMARY KEY', 'name': 'TEXT NOT NULL'}).")]
) -> str:
    """Create a new table in the local SQLite database."""
    if not isinstance(table_name, str) or not table_name.isidentifier():
        return "Error: Invalid table name format."

    if not isinstance(columns, dict) or not columns:
        return "Error: 'columns' must be a non-empty JSON object."

    # Validate column names and definitions to prevent SQL injection
    column_definitions = []
    for col_name, col_def in columns.items():
        if not isinstance(col_name, str) or not col_name.isidentifier():
            return f"Error: Invalid column name format for '{col_name}'."
        if not isinstance(col_def, str) or not col_def.strip():
            return f"Error: Invalid column definition for '{col_name}'."
        
        column_definitions.append(f"{col_name} {col_def}")

    if not os.path.exists(DB_PATH):
        return f"Error: Database file '{DB_PATH}' does not exist locally."

    conn = None

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cols_str = ", ".join(column_definitions)
        sql = f"CREATE TABLE IF NOT EXISTS {table_name} ({cols_str})"

        cursor.execute(sql)
        conn.commit()

        return f"Successfully created table '{table_name}'."

    except sqlite3.Error as e:
        if conn:
            conn.rollback()
        return f"SQLite Error: {str(e)}"

    except Exception as e:
        if conn:
            conn.rollback()
        return f"Unexpected Error: {str(e)}"

    finally:
        if conn:
            conn.close()


@mcp.tool(
    name="get_first_record",
    description="Get the very first record from a specified local SQLite table."
)
def get_first_record(
    table_name: Annotated[str, Field(description="The name of the SQLite table to query.")]
) -> str:
    """Get the very first record from a specified local SQLite table."""
    if not isinstance(table_name, str) or not table_name.isidentifier():
        return "Error: Invalid table name format."

    if not os.path.exists(DB_PATH):
        return f"Error: Database file '{DB_PATH}' does not exist locally."

    conn = None

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute(f"SELECT * FROM {table_name} LIMIT 1")

        record = cursor.fetchone()

        if record:
            result_str = json.dumps(dict(record), indent=2)
        else:
            result_str = f"Table '{table_name}' is empty or has no records."

        return result_str

    except sqlite3.OperationalError as e:
        return f"SQLite Error: {str(e)}"

    except Exception as e:
        return f"Unexpected Error: {str(e)}"

    finally:
        if conn:
            conn.close()


@mcp.tool(
    name="insert_row",
    description="Insert a new record into a specified local SQLite table."
)
def insert_row(
    table_name: Annotated[str, Field(description="The name of the SQLite table to insert a record into.")],
    data: Annotated[dict, Field(description="A JSON object mapping column names to their values.")]
) -> str:
    """Insert a new record into a specified local SQLite table."""
    if not isinstance(table_name, str) or not table_name.isidentifier():
        return "Error: Invalid table name format."

    if not isinstance(data, dict) or not data:
        return "Error: 'data' must be a non-empty JSON object."

    for col in data.keys():
        if not isinstance(col, str) or not col.isidentifier():
            return f"Error: Invalid column name format for '{col}'."

    if not os.path.exists(DB_PATH):
        return f"Error: Database file '{DB_PATH}' does not exist locally."

    conn = None

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        columns = list(data.keys())
        values = list(data.values())

        columns_str = ", ".join(columns)
        placeholders_str = ", ".join(["?"] * len(columns))

        sql = f"INSERT INTO {table_name} ({columns_str}) VALUES ({placeholders_str})"

        cursor.execute(sql, values)
        conn.commit()

        last_id = cursor.lastrowid

        return f"Successfully inserted record into '{table_name}'. Last Inserted Row ID: {last_id}"

    except sqlite3.Error as e:
        if conn:
            conn.rollback()
        return f"SQLite Error: {str(e)}"

    except Exception as e:
        if conn:
            conn.rollback()
        return f"Unexpected Error: {str(e)}"

    finally:
        if conn:
            conn.close()


@mcp.tool(
    name="update_row",
    description="Update existing records in a specified local SQLite table based on equality conditions."
)
def update_row(
    table_name: Annotated[str, Field(description="The name of the SQLite table to update.")],
    data: Annotated[dict, Field(description="A JSON object mapping column names to their new values.")],
    conditions: Annotated[dict, Field(description="A JSON object mapping column names to their exact values to match (WHERE clause).")]
) -> str:
    """Update existing records in a specified local SQLite table."""
    if not isinstance(table_name, str) or not table_name.isidentifier():
        return "Error: Invalid table name format."

    if not isinstance(data, dict) or not data:
        return "Error: 'data' must be a non-empty JSON object."

    if not isinstance(conditions, dict) or not conditions:
        return "Error: 'conditions' must be a non-empty JSON object."

    # Validate column names to prevent SQL injection
    for col in data.keys():
        if not isinstance(col, str) or not col.isidentifier():
            return f"Error: Invalid column name format in data for '{col}'."

    for col in conditions.keys():
        if not isinstance(col, str) or not col.isidentifier():
            return f"Error: Invalid column name format in conditions for '{col}'."

    if not os.path.exists(DB_PATH):
        return f"Error: Database file '{DB_PATH}' does not exist locally."

    conn = None

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Construct the SET clause
        set_clauses = [f"{col} = ?" for col in data.keys()]
        set_str = ", ".join(set_clauses)

        # Construct the WHERE clause (joining conditions with AND)
        where_clauses = [f"{col} = ?" for col in conditions.keys()]
        where_str = " AND ".join(where_clauses)

        # Combine values for the prepared statement
        values = list(data.values()) + list(conditions.values())

        sql = f"UPDATE {table_name} SET {set_str} WHERE {where_str}"

        cursor.execute(sql, values)
        conn.commit()

        affected_rows = cursor.rowcount

        if affected_rows > 0:
            return f"Successfully updated '{table_name}'. Rows affected: {affected_rows}"
        else:
            return f"No rows were updated in '{table_name}'. (No records matched the conditions)"

    except sqlite3.Error as e:
        if conn:
            conn.rollback()
        return f"SQLite Error: {str(e)}"

    except Exception as e:
        if conn:
            conn.rollback()
        return f"Unexpected Error: {str(e)}"

    finally:
        if conn:
            conn.close()


@mcp.tool(
    name="read_query",
    description=(
        "Run a read-only SQLite SELECT or WITH query and return "
        "the results as a Markdown table. The query cannot modify "
        "the database. The limit parameter controls the maximum "
        "number of rows returned."
    )
)
def read_query(
    sql: Annotated[
        str,
        Field(
            description=(
                "A read-only SQLite SELECT or WITH SQL query. "
                "INSERT, UPDATE, DELETE, DROP, ALTER and other "
                "modifying statements are not allowed."
            )
        )
    ],
    limit: Annotated[
        int,
        Field(
            description="Maximum number of rows to return.",
            ge=1,
            le=1000
        )
    ] = 100
) -> str:
    """Run a read-only SQLite SELECT or WITH query and return the results as a Markdown table."""
    if not isinstance(sql, str):
        return "Error: 'sql' must be a string."

    if not isinstance(limit, int):
        return "Error: 'limit' must be an integer."

    if limit < 1 or limit > 1000:
        return "Error: 'limit' must be between 1 and 1000."

    valid, validated_sql = validate_read_query(sql)

    if not valid:
        return f"Error: {validated_sql}"

    if not os.path.exists(DB_PATH):
        return f"Error: Database file '{DB_PATH}' does not exist locally."

    conn = None

    try:
        db_uri = f"file:{os.path.abspath(DB_PATH)}?mode=ro"

        conn = sqlite3.connect(db_uri, uri=True)
        conn.set_authorizer(readonly_authorizer)

        cursor = conn.cursor()

        limited_sql = f"SELECT * FROM ({validated_sql}) LIMIT {limit}"

        cursor.execute(limited_sql)

        rows = cursor.fetchall()

        result_str = rows_to_markdown(cursor, rows)

        result_str = f"Returned {len(rows)} row(s).\n\n" + result_str

        return result_str

    except sqlite3.DatabaseError as e:
        return f"SQLite Error: {str(e)}"

    except Exception as e:
        return f"Unexpected Error: {str(e)}"

    finally:
        if conn:
            conn.close()


# 4. Setup Starlette application instance from FastMCP
app = mcp.sse_app()


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

    #mcp.run(transport="sse")
    mcp.run(transport="sse", host="0.0.0.0", port=PORT)