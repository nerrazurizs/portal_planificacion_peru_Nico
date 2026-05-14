from snowflake.snowpark.context import get_active_session


def get_active_connection():
    """Return the underlying connector connection from the active Snowpark session.

    In Streamlit in Snowflake, authentication and session management are handled
    by the platform — no credentials or reconnect logic required.
    """
    return get_active_session().connection


# Backward-compatible alias (app.py imports both names)
get_snowflake_connection = get_active_connection
