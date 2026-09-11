using FluentValidation;
using Microsoft.AspNetCore.Diagnostics;
using Microsoft.AspNetCore.Mvc;
using Sentinel.Application.Common;
using Sentinel.Application.Features.Auth;

namespace Sentinel.Api;

/// <summary>
/// Turns application exceptions into RFC 7807 problem responses.
/// </summary>
/// <remarks>
/// Only exception types the application defines get a specific status code. Everything else
/// becomes a 500 and is logged at Error, on purpose: mapping unknown exceptions to 4xx is how a
/// real fault ends up looking like a client mistake and never gets investigated.
/// </remarks>
public sealed class SentinelExceptionHandler : IExceptionHandler
{
    private readonly IProblemDetailsService _problemDetails;
    private readonly ILogger<SentinelExceptionHandler> _logger;

    public SentinelExceptionHandler(
        IProblemDetailsService problemDetails,
        ILogger<SentinelExceptionHandler> logger)
    {
        _problemDetails = problemDetails;
        _logger = logger;
    }

    public async ValueTask<bool> TryHandleAsync(
        HttpContext httpContext,
        Exception exception,
        CancellationToken cancellationToken)
    {
        var (status, title, errors) = Map(exception);

        if (status >= StatusCodes.Status500InternalServerError)
        {
            _logger.LogError(exception, "Unhandled exception on {Method} {Path}",
                httpContext.Request.Method, httpContext.Request.Path);
        }
        else
        {
            _logger.LogInformation("{Title} on {Method} {Path}: {Message}",
                title, httpContext.Request.Method, httpContext.Request.Path, exception.Message);
        }

        httpContext.Response.StatusCode = status;

        var problem = new ProblemDetails
        {
            Status = status,
            Title = title,
            // A 500's message can carry connection strings or internal paths, so it is logged
            // rather than returned.
            Detail = status >= StatusCodes.Status500InternalServerError
                ? "An unexpected error occurred."
                : exception.Message,
            Instance = httpContext.Request.Path,
        };

        if (errors is not null)
        {
            problem.Extensions["errors"] = errors;
        }

        return await _problemDetails.TryWriteAsync(new ProblemDetailsContext
        {
            HttpContext = httpContext,
            Exception = exception,
            ProblemDetails = problem,
        });
    }

    private static (int Status, string Title, IDictionary<string, string[]>? Errors) Map(Exception exception) =>
        exception switch
        {
            ValidationException validation => (
                StatusCodes.Status400BadRequest,
                "Validation failed",
                validation.Errors
                    .GroupBy(e => e.PropertyName)
                    .ToDictionary(g => g.Key, g => g.Select(e => e.ErrorMessage).ToArray())),

            UnauthorizedException => (StatusCodes.Status401Unauthorized, "Unauthorized", null),
            ForbiddenException => (StatusCodes.Status403Forbidden, "Forbidden", null),
            NotFoundException => (StatusCodes.Status404NotFound, "Not found", null),
            ConflictException => (StatusCodes.Status409Conflict, "Conflict", null),

            // A dependency of ours failed, not the caller's request. 502 rather than 500 so the
            // frontend can say which part of the stack is down instead of "something broke".
            AiServiceUnavailableException => (
                StatusCodes.Status502BadGateway, "The AI service is unavailable", null),

            _ => (StatusCodes.Status500InternalServerError, "Internal server error", null),
        };
}
