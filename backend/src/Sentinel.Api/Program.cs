using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.EntityFrameworkCore;
using Microsoft.OpenApi.Models;
using Sentinel.Api;
using Sentinel.Application;
using Sentinel.Infrastructure;
using Sentinel.Infrastructure.Persistence;
using Serilog;

var builder = WebApplication.CreateBuilder(args);

// Structured logging from the start: in Phase 2 these events go to Loki through the OTel
// collector, and the agent reads the platform's own logs the same way it reads a service's.
builder.Host.UseSerilog((context, configuration) => configuration
    .ReadFrom.Configuration(context.Configuration)
    .Enrich.FromLogContext()
    .WriteTo.Console());

builder.Services.AddApplication();
builder.Services.AddInfrastructure(builder.Configuration);

builder.Services.AddControllers()
    .AddJsonOptions(options =>
    {
        // snake_case across the wire, so the Python AI service and the .NET backend agree on one
        // convention rather than translating at every boundary.
        options.JsonSerializerOptions.PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower;
        options.JsonSerializerOptions.DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull;
        options.JsonSerializerOptions.Converters.Add(new JsonStringEnumConverter(JsonNamingPolicy.SnakeCaseLower));
    });

builder.Services.AddProblemDetails();
builder.Services.AddExceptionHandler<SentinelExceptionHandler>();

builder.Services.AddEndpointsApiExplorer();
builder.Services.AddSwaggerGen(options =>
{
    options.SwaggerDoc("v1", new OpenApiInfo
    {
        Title = "SentinelAI API",
        Version = "v1",
        Description = "Autonomous incident investigation and resolution platform.",
    });

    options.AddSecurityDefinition("Bearer", new OpenApiSecurityScheme
    {
        Name = "Authorization",
        Type = SecuritySchemeType.Http,
        Scheme = "bearer",
        BearerFormat = "JWT",
        In = ParameterLocation.Header,
        Description = "Paste the access_token returned by POST /api/auth/login.",
    });

    options.AddSecurityRequirement(new OpenApiSecurityRequirement
    {
        [new OpenApiSecurityScheme
        {
            Reference = new OpenApiReference { Type = ReferenceType.SecurityScheme, Id = "Bearer" },
        }] = [],
    });
});

const string FrontendCors = "frontend";
builder.Services.AddCors(options => options.AddPolicy(FrontendCors, policy => policy
    .WithOrigins(builder.Configuration["Cors:AllowedOrigins"]?.Split(',') ?? ["http://localhost:5173"])
    .AllowAnyHeader()
    .AllowAnyMethod()
    // Required for the SignalR hub added in Phase 7.
    .AllowCredentials()));

var app = builder.Build();

await MigrateAndSeedAsync(app);

app.UseExceptionHandler();
app.UseSerilogRequestLogging();

if (app.Environment.IsDevelopment())
{
    app.UseSwagger();
    app.UseSwaggerUI(options => options.SwaggerEndpoint("/swagger/v1/swagger.json", "SentinelAI v1"));
}

app.UseCors(FrontendCors);
app.UseAuthentication();
app.UseAuthorization();

app.MapControllers();

app.MapGet("/health", () => Results.Ok(new { status = "up" })).AllowAnonymous();

app.Run();

/// <summary>
/// Applies migrations and seeds on startup.
/// </summary>
/// <remarks>
/// Acceptable here because the whole platform is a local single-instance stack and the README
/// promises a working system from one command. A deployment with more than one replica would
/// need this to move to a job that runs once.
/// </remarks>
static async Task MigrateAndSeedAsync(WebApplication app)
{
    await using var scope = app.Services.CreateAsyncScope();

    var db = scope.ServiceProvider.GetRequiredService<SentinelDbContext>();
    await db.Database.MigrateAsync();

    var seeder = scope.ServiceProvider.GetRequiredService<DbSeeder>();
    await seeder.SeedAsync();
}

/// <summary>Exposed so the integration test project can build a test host from this entry point.</summary>
public partial class Program;
