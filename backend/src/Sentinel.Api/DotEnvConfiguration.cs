namespace Sentinel.Api;

/// <summary>
/// Reads the repository's <c>.env</c> when the backend is run on the host, so that one file
/// configures the whole stack.
/// </summary>
/// <remarks>
/// The AI service already does this — pydantic-settings reads the same file — and its config
/// module gives the reason: "one password in .env, not two that can drift apart". The backend was
/// the half that did not, and the drift it allowed was not hypothetical. `.env.example` tells the
/// reader that a machine with its own PostgreSQL on 5432 should "pick a free port here", which
/// moves the AI service and Alembic onto the new port and leaves the backend pointed at whatever
/// owns 5432, where it fails with <c>password authentication failed for user "sentinel"</c> and
/// says nothing about ports at all.
///
/// Three rules, and each one is what keeps this from being a surprise:
///
/// *Real environment variables win.* Anything already set is left alone, so compose — which
/// passes <c>ConnectionStrings__Postgres</c> directly — is unaffected, and so is anyone
/// exporting a variable to override the file for one run.
///
/// *It is registered below the existing sources but above appsettings*, so a value in
/// <c>appsettings.Development.json</c> is a default rather than a ceiling.
///
/// *A missing file is not an error.* A container has no <c>.env</c> and does not need one.
/// </remarks>
public static class DotEnvConfiguration
{
    /// <summary>
    /// Adds <c>POSTGRES_*</c> and friends from the repository root's <c>.env</c>, if it is there.
    /// </summary>
    public static WebApplicationBuilder AddRepositoryDotEnv(this WebApplicationBuilder builder)
    {
        var path = FindDotEnv(builder.Environment.ContentRootPath);

        if (path is null)
        {
            return builder;
        }

        var values = ReadConfiguration(File.ReadAllLines(path));

        if (values.Count > 0)
        {
            builder.Configuration.AddInMemoryCollection(values!);
        }

        return builder;
    }

    /// <summary>
    /// The whole translation from <c>.env</c> lines to configuration keys, in one place so that
    /// it can be tested without a file or a host.
    /// </summary>
    public static Dictionary<string, string> ReadConfiguration(IEnumerable<string> lines)
    {
        var values = Parse(lines);

        if (ConnectionStringFrom(values) is { } connectionString)
        {
            values["ConnectionStrings:Postgres"] = connectionString;
        }

        return values;
    }

    /// <summary>
    /// Assembles the PostgreSQL connection string from the <c>POSTGRES_*</c> variables, the way
    /// the AI service does.
    /// </summary>
    /// <remarks>
    /// This is the part that matters. Loading <c>.env</c> alone would put <c>POSTGRES_PORT</c>
    /// into configuration where nothing reads it, and the connection string in
    /// <c>appsettings.Development.json</c> — which writes the port out longhand — would still
    /// win. Assembling it here is what makes moving the port a one-line change.
    ///
    /// Returns null unless the file actually carries the parts, so a <c>.env</c> that configures
    /// something else entirely does not replace a connection string somebody set deliberately.
    /// An explicit <c>ConnectionStrings__Postgres</c> in the file is left alone: it is more
    /// specific than the pieces, and compose passes exactly that.
    /// </remarks>
    private static string? ConnectionStringFrom(Dictionary<string, string> values)
    {
        if (values.ContainsKey("ConnectionStrings:Postgres"))
        {
            return null;
        }

        if (!values.TryGetValue("POSTGRES_USER", out var user)
            || !values.TryGetValue("POSTGRES_PASSWORD", out var password)
            || !values.TryGetValue("POSTGRES_DB", out var database))
        {
            return null;
        }

        var host = values.GetValueOrDefault("POSTGRES_HOST", "localhost");
        var port = values.GetValueOrDefault("POSTGRES_PORT", "5432");

        return $"Host={host};Port={port};Database={database};Username={user};Password={password}";
    }

    /// <summary>
    /// Walks up from the content root looking for <c>.env</c>.
    /// </summary>
    /// <remarks>
    /// The backend runs from <c>backend/src/Sentinel.Api</c>, so the file is three levels up; the
    /// walk is bounded rather than counted because <c>dotnet run</c>, <c>dotnet watch</c> and a
    /// published build all disagree about where the content root is.
    /// </remarks>
    private static string? FindDotEnv(string start)
    {
        for (var directory = new DirectoryInfo(start); directory is not null; directory = directory.Parent)
        {
            var candidate = Path.Combine(directory.FullName, ".env");

            if (File.Exists(candidate))
            {
                return candidate;
            }
        }

        return null;
    }

    /// <summary>
    /// Turns <c>KEY=value</c> lines into configuration keys, skipping anything already in the
    /// environment.
    /// </summary>
    /// <remarks>
    /// Double underscores become colons, which is .NET's own convention, so
    /// <c>ConnectionStrings__Postgres</c> in the file reaches
    /// <c>Configuration["ConnectionStrings:Postgres"]</c> exactly as it would from a real
    /// environment variable. Quotes around a value are stripped because compose accepts them and
    /// people write them.
    /// </remarks>
    private static Dictionary<string, string> Parse(IEnumerable<string> lines)
    {
        var values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        foreach (var line in lines)
        {
            var trimmed = line.Trim();

            if (trimmed.Length == 0 || trimmed.StartsWith('#'))
            {
                continue;
            }

            var separator = trimmed.IndexOf('=');

            if (separator <= 0)
            {
                continue;
            }

            var key = trimmed[..separator].Trim();
            var value = trimmed[(separator + 1)..].Trim().Trim('"');

            // Already set by the real environment, which outranks the file.
            if (!string.IsNullOrEmpty(Environment.GetEnvironmentVariable(key)))
            {
                continue;
            }

            values[key.Replace("__", ":")] = value;
        }

        return values;
    }
}
