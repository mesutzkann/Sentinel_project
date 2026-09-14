using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.Extensions.Configuration;
using Sentinel.Application.Common;

namespace Sentinel.Infrastructure.Ai;

/// <summary>
/// Mints the token a human's approval turns into.
/// </summary>
/// <remarks>
/// <para>
/// The format is defined by <c>ai-service/mcp_client/approval.py</c> and read by two other
/// processes: the AI service's policy layer and the MCP server that does the work. It is an
/// HMAC-SHA256 over a canonical JSON payload, and "canonical" is load-bearing — the claims are
/// serialised with sorted keys and no whitespace, because the signature covers the bytes. A
/// property written in a different order here produces a token that verifies nowhere.
/// </para>
/// <para>
/// So the payload is built by hand rather than by <see cref="JsonSerializer"/>: the serialiser's
/// key order is its own business and this one is part of a wire contract. The keys are
/// <c>args, by, exp, rec, tool, v</c>, alphabetical, which is what Python's
/// <c>json.dumps(sort_keys=True, separators=(",", ":"))</c> produces.
/// </para>
/// <para>
/// **A grant names one action.** The recommendation, the qualified tool name, a SHA-256 of the
/// arguments and an expiry. An approval of "restart the orders container" therefore cannot be
/// replayed as anything else — the verifier compares all four against the call being made.
/// </para>
/// </remarks>
public sealed class ApprovalTokenService : IApprovalTokenService
{
    /// <summary>Shared with the AI service and the MCP servers. Without it, nothing is approvable.</summary>
    public const string ConfigurationKey = "Internal:ApprovalSecret";

    /// <summary>The payload's scheme tag. A token of another scheme must fail rather than be reinterpreted.</summary>
    public const string Scheme = "sa1";

    /// <summary>
    /// Minutes, not hours. A derived token cannot be revoked before it expires — that is what it
    /// trades for needing no row and no lookup — so the window is the whole mitigation, and an
    /// approval describes the system the person was looking at when they gave it.
    /// </summary>
    public static readonly TimeSpan DefaultLifetime = TimeSpan.FromMinutes(10);

    private readonly byte[] _key;

    public ApprovalTokenService(IConfiguration configuration)
    {
        var secret = configuration[ConfigurationKey] ?? configuration["APPROVAL_SECRET"];

        _key = string.IsNullOrWhiteSpace(secret) ? [] : Encoding.UTF8.GetBytes(secret);
    }

    public bool IsConfigured => _key.Length > 0;

    public string Issue(
        Guid recommendationId,
        string tool,
        string? argumentsJson,
        string? approvedBy,
        DateTimeOffset? expiresAt = null)
    {
        if (_key.Length == 0)
        {
            // The same refusal the callback tokens make. An unset secret must not quietly become
            // a token anybody can mint for any action.
            throw new InvalidOperationException(
                $"{ConfigurationKey} is not configured, so no action can be approved.");
        }

        var expiry = (expiresAt ?? DateTimeOffset.UtcNow.Add(DefaultLifetime)).ToUnixTimeSeconds();
        var payload = Base64Url(Encoding.UTF8.GetBytes(Canonical(
            recommendationId,
            tool,
            HashArguments(argumentsJson),
            expiry,
            approvedBy)));

        return $"{payload}.{Base64Url(HMACSHA256.HashData(_key, Encoding.UTF8.GetBytes(payload)))}";
    }

    /// <summary>
    /// SHA-256 of the arguments, in the same canonical form the Python side hashes.
    /// </summary>
    /// <remarks>
    /// The arguments are hashed rather than carried: they can hold a connection string or a
    /// patch, and a token travels in logs and in URLs. Re-serialising through a sorted
    /// dictionary rather than hashing the stored string directly, because the stored JSON came
    /// from the agent and its key order is not something this side gets to depend on.
    /// </remarks>
    public static string HashArguments(string? argumentsJson)
    {
        var arguments = Parse(argumentsJson);
        var canonical = new StringBuilder("{");

        for (var index = 0; index < arguments.Count; index++)
        {
            if (index > 0)
            {
                canonical.Append(',');
            }

            var (key, value) = arguments[index];
            canonical.Append(JsonSerializer.Serialize(key)).Append(':').Append(value);
        }

        canonical.Append('}');

        // Lower case, because the Python side writes `hashlib.sha256(...).hexdigest()` and the
        // hash is compared as a string.
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(canonical.ToString())))
            .ToLowerInvariant();
    }

    /// <summary>
    /// The payload, byte for byte as Python writes it: sorted keys, no spaces, optional
    /// <c>by</c> omitted rather than null.
    /// </summary>
    private static string Canonical(
        Guid recommendationId,
        string tool,
        string argumentsHash,
        long expiry,
        string? approvedBy)
    {
        var claims = new StringBuilder("{");

        claims.Append("\"args\":").Append(JsonSerializer.Serialize(argumentsHash));

        if (!string.IsNullOrWhiteSpace(approvedBy))
        {
            claims.Append(",\"by\":").Append(JsonSerializer.Serialize(approvedBy));
        }

        claims.Append(",\"exp\":").Append(expiry.ToString(CultureInfo.InvariantCulture));
        claims.Append(",\"rec\":").Append(JsonSerializer.Serialize(recommendationId.ToString()));
        claims.Append(",\"tool\":").Append(JsonSerializer.Serialize(tool));
        claims.Append(",\"v\":").Append(JsonSerializer.Serialize(Scheme));
        claims.Append('}');

        return claims.ToString();
    }

    /// <summary>The arguments as sorted (key, compact-JSON-value) pairs, or empty for anything unusable.</summary>
    private static List<(string Key, string Value)> Parse(string? argumentsJson)
    {
        if (string.IsNullOrWhiteSpace(argumentsJson))
        {
            return [];
        }

        try
        {
            using var document = JsonDocument.Parse(argumentsJson);

            if (document.RootElement.ValueKind is not JsonValueKind.Object)
            {
                return [];
            }

            return document.RootElement.EnumerateObject()
                // Ordinal, which is what Python's sort_keys does: a culture-aware sort puts
                // "Z" before "a" in some locales and the signature would depend on the server's.
                .OrderBy(property => property.Name, StringComparer.Ordinal)
                .Select(property => (property.Name, Compact(property.Value)))
                .ToList();
        }
        catch (JsonException)
        {
            return [];
        }
    }

    /// <summary>One value, serialised the way Python's compact separators write it.</summary>
    private static string Compact(JsonElement value) => value.ValueKind switch
    {
        JsonValueKind.String => JsonSerializer.Serialize(value.GetString()),
        JsonValueKind.True => "true",
        JsonValueKind.False => "false",
        JsonValueKind.Null => "null",
        JsonValueKind.Number => value.GetRawText(),
        _ => value.GetRawText().Replace(", ", ",", StringComparison.Ordinal),
    };

    /// <summary>URL-safe and unpadded, because a token ends up in a URL sooner or later.</summary>
    private static string Base64Url(byte[] raw) =>
        Convert.ToBase64String(raw).TrimEnd('=').Replace('+', '-').Replace('/', '_');
}
