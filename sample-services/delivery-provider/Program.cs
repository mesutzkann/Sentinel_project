// The third-party delivery provider the notifications service sends through: an SMTP relay, an
// SMS gateway, a push service. It exists so that chaos scenario 12
// (EXTERNAL_DEPENDENCY_UNAVAILABLE) has something genuinely outside the service boundary to fail.
//
// **It is deliberately not instrumented, and that is the whole design.** The five sample services
// export OTLP and appear in Jaeger as named services; this one does not, so a request that fails
// here terminates at the outgoing HTTP client span in notifications with nothing below it. That
// is the scenario's discriminator — the failing span is outside the boundary — and instrumenting
// this process would erase it by turning the provider into a sixth service that looks exactly
// like an internal dependency. It is also why nothing here references the shared project: no
// telemetry, no chaos registry, no database, no OpenTelemetry packages at all.

var builder = WebApplication.CreateBuilder(args);

var app = builder.Build();

// Whether the provider is currently refusing. Flipped by the notifications service when
// scenario 12 is enabled and disabled, so the dependency really does go down and come back
// rather than the caller pretending it did.
var unavailable = false;

app.MapPost("/send", (DeliveryRequest request) =>
{
    if (unavailable)
    {
        // A 503 with a Retry-After, which is what a real relay under load answers with, and what
        // makes the caller's retries reasonable rather than impatient.
        return Results.Json(
            new { error = "The delivery provider is temporarily unavailable.", retry_after = 30 },
            statusCode: StatusCodes.Status503ServiceUnavailable);
    }

    return Results.Ok(new { accepted = true, recipient = request.Recipient });
});

// The control plane, not part of the provider's contract. Separate from /send so that nothing on
// the delivery path can change availability by accident.
app.MapPost("/_control/availability", (AvailabilityRequest request) =>
{
    unavailable = request.Unavailable;

    return Results.Ok(new { unavailable });
});

app.MapGet("/health/live", () => Results.Ok(new { status = "up" }));

app.Run();

internal sealed record DeliveryRequest(string Recipient, string Subject, string Body);

internal sealed record AvailabilityRequest(bool Unavailable);
