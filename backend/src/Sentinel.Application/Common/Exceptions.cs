namespace Sentinel.Application.Common;

/// <summary>
/// Base for errors the API translates into a specific status code. Anything not derived from
/// this is a bug and becomes a 500 — the distinction matters, because silently turning unexpected
/// failures into 4xx is how real problems get hidden.
/// </summary>
public abstract class SentinelException : Exception
{
    protected SentinelException(string message) : base(message)
    {
    }
}

/// <summary>Asked for something that does not exist. Maps to 404.</summary>
public sealed class NotFoundException : SentinelException
{
    public NotFoundException(string entity, object key)
        : base($"{entity} '{key}' was not found.")
    {
    }
}

/// <summary>The request conflicts with existing state, such as a duplicate service name. Maps to 409.</summary>
public sealed class ConflictException : SentinelException
{
    public ConflictException(string message) : base(message)
    {
    }
}

/// <summary>The caller is authenticated but not allowed to do this. Maps to 403.</summary>
public sealed class ForbiddenException : SentinelException
{
    public ForbiddenException(string message) : base(message)
    {
    }
}
