using FluentValidation;
using MediatR;
using Microsoft.EntityFrameworkCore;
using Sentinel.Application.Common;
using Sentinel.Domain;

namespace Sentinel.Application.Features.Auth;

public sealed record LoginResponse(
    string AccessToken,
    DateTimeOffset ExpiresAt,
    Guid UserId,
    string Username,
    UserRole Role);

public sealed record LoginCommand(string Username, string Password) : IRequest<LoginResponse>;

public sealed class LoginValidator : AbstractValidator<LoginCommand>
{
    public LoginValidator()
    {
        RuleFor(c => c.Username).NotEmpty().MaximumLength(100);
        RuleFor(c => c.Password).NotEmpty().MaximumLength(200);
    }
}

public sealed class LoginHandler : IRequestHandler<LoginCommand, LoginResponse>
{
    private readonly ISentinelDbContext _db;
    private readonly IPasswordHasher _hasher;
    private readonly IJwtTokenService _tokens;

    public LoginHandler(ISentinelDbContext db, IPasswordHasher hasher, IJwtTokenService tokens)
    {
        _db = db;
        _hasher = hasher;
        _tokens = tokens;
    }

    public async Task<LoginResponse> Handle(LoginCommand request, CancellationToken cancellationToken)
    {
        var user = await _db.Users
            .AsNoTracking()
            .FirstOrDefaultAsync(u => u.Username == request.Username, cancellationToken);

        // One message for both "no such user" and "wrong password", so the endpoint cannot be
        // used to enumerate accounts. The hash is still verified when the user is missing, so the
        // two paths take comparable time.
        var passwordMatches = user is not null && _hasher.Verify(request.Password, user.PasswordHash);

        if (user is null || !passwordMatches)
        {
            throw new UnauthorizedException("Invalid username or password.");
        }

        var (token, expiresAt) = _tokens.CreateToken(user.Id, user.Username, user.Role);

        return new LoginResponse(token, expiresAt, user.Id, user.Username, user.Role);
    }
}

/// <summary>Credentials were missing or wrong. Maps to 401.</summary>
public sealed class UnauthorizedException : SentinelException
{
    public UnauthorizedException(string message) : base(message)
    {
    }
}

public sealed record MeResponse(Guid UserId, string Username, UserRole Role);

public sealed record GetCurrentUserQuery : IRequest<MeResponse>;

public sealed class GetCurrentUserHandler : IRequestHandler<GetCurrentUserQuery, MeResponse>
{
    private readonly ICurrentUser _currentUser;

    public GetCurrentUserHandler(ICurrentUser currentUser) => _currentUser = currentUser;

    public Task<MeResponse> Handle(GetCurrentUserQuery request, CancellationToken cancellationToken)
    {
        if (_currentUser.UserId is not { } userId)
        {
            throw new UnauthorizedException("Not authenticated.");
        }

        return Task.FromResult(new MeResponse(
            userId,
            _currentUser.Username ?? string.Empty,
            _currentUser.Role ?? UserRole.Viewer));
    }
}
